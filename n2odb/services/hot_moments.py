"""Hot-moment (flux outlier) detection → the HMMAD / HMMCD columns.

Shared by the ``hot_moment_detection`` management command (whole database) and
workbench promotion (just the newly promoted treatments). Each treatment is
scored independently, so running it on a subset gives the same flags as a full run.
"""
from collections import defaultdict

import numpy as np
import pandas as pd
from pyod.models.mad import MAD
from pyod.models.mcd import MCD

from ..models import RawMeasurementTreatment


def detect(treatment_ids=None, log=lambda msg: None) -> tuple[int, dict]:
    """Score fluxes per treatment and write hmmad/hmmcd.

    ``treatment_ids=None`` scores every treatment. Returns ``(rows_updated, failed)``
    where ``failed`` maps treatment id → error for treatments the models couldn't
    score (their rows are left untouched).
    """
    qs = RawMeasurementTreatment.objects.all()
    if treatment_ids is not None:
        qs = qs.filter(treatmentid__in=treatment_ids)
    qs = qs.values('rawtreatmentid', 'date', 'treatmentid', 'n2oflux', 'hmmad', 'hmmcd')

    df = pd.DataFrame(list(qs))
    if df.empty:
        return 0, {}
    df = df.dropna(subset=['n2oflux'])

    log("Calculating outliers grouped by Treatment...")

    # Create a list to hold our fully processed chunks of data
    processed_groups = []
    failed = {}

    for treatment_id, group in df.groupby('treatmentid'):
        mad_threshold = 2.025
        mcd_treshold = 17

        # Sort by date to maintain temporal order
        id_data = group.sort_values('date')
        flux = id_data[['n2oflux']].copy()
        flux['index'] = np.arange(len(flux))

        # A lone measurement can't be an outlier relative to its own series (and
        # MCD needs >= 2 samples), so flag it as not a hot moment.
        if len(flux) < 2:
            id_data['hmmad'] = 0
            id_data['hmmcd'] = 0
            processed_groups.append(id_data)
            continue

        try:
            mad = MAD(threshold=1.2)
            mad.fit(id_data[['n2oflux']])
            mad_scores = mad.decision_scores_
            mad_pred = [1 if score > mad_threshold else 0 for score in mad_scores]

            mcd = MCD(contamination=0.5, random_state=0)  # pinned so HMMCD is reproducible
            mcd.fit(flux)
            mcd_scores = mcd.decision_scores_
            mcd_pred = [1 if score > mcd_treshold else 0 for score in mcd_scores]
        except Exception as exc:  # noqa: BLE001 — leave just this treatment blank
            failed[treatment_id] = str(exc)
            continue

        median = np.median(flux['n2oflux'])
        # If flux is less than median but flagged as outlier, set to non-outlier (to avoid false positives on low flux values)
        mad_pred = [0 if (flux.iloc[i]['n2oflux'] < median and mad_pred[i] == 1) else mad_pred[i] for i in range(len(flux))]
        mcd_pred = [0 if (flux.iloc[i]['n2oflux'] < median and mcd_pred[i] == 1) else mcd_pred[i] for i in range(len(flux))]

        # If flux is less than 1, set to non-outlier (to avoid false positives on very low flux values)
        mad_pred = [0 if (flux.iloc[i]['n2oflux'] < 1 and mad_pred[i] == 1) else mad_pred[i] for i in range(len(flux))]
        mcd_pred = [0 if (flux.iloc[i]['n2oflux'] < 1 and mcd_pred[i] == 1) else mcd_pred[i] for i in range(len(flux))]

        id_data['hmmad'] = mad_pred
        id_data['hmmcd'] = mcd_pred

        # Add the finished chunk to our list
        processed_groups.append(id_data)

    if not processed_groups:
        return 0, failed
    df = pd.concat(processed_groups, ignore_index=True)

    log(f"Writing {len(df)} records to the database...")

    # NOTE: do NOT use bulk_update() here. It emits a single
    #   UPDATE ... SET hmmad = CASE WHEN pk=.. THEN .. ... END, hmmcd = CASE ... END
    # with one WHEN branch per row per column. At ~79k rows that statement is huge
    # enough to exhaust the db container's memory and get mysqld OOM-killed
    # mid-query (the "Lost connection (2013)" then "Can't connect (2002)" you saw).
    #
    # hmmad/hmmcd are two booleans, so there are only 4 possible result
    # combinations. Group the PKs by combination and issue one small, chunked
    # UPDATE ... WHERE pk IN (...) per group — cheap, and safe to re-run.
    buckets = defaultdict(list)
    for pk, mad_val, mcd_val in zip(df['rawtreatmentid'], df['hmmad'], df['hmmcd']):
        buckets[(bool(mad_val), bool(mcd_val))].append(pk)

    CHUNK = 2000
    total = 0
    for (mad_val, mcd_val), pks in buckets.items():
        for i in range(0, len(pks), CHUNK):
            total += RawMeasurementTreatment.objects.filter(
                rawtreatmentid__in=pks[i:i + CHUNK]
            ).update(hmmad=mad_val, hmmcd=mcd_val)
    return total, failed
