# utils.py or top of views.py
class Echo:
    """An object that implements just the write method of the file-like
    interface.
    """
    def write(self, value):
        return value

# ### ANALYTICS INSERTION: HELPER FUNCTION ###
def _log_user_action(request, action_type, payload, email=None, ids=None, count=None):
    try:
        from .models import UserAction 
        
        if hasattr(request, 'user_agent') and request.user_agent.is_bot:
            return

        if not request.session.session_key:
            request.session.save()

        # Save to DB with the specific email column populated
        UserAction.objects.create(
            session_key=request.session.session_key,
            action_type=action_type,
            filters=payload,
            user_email=email,
            result_count=count,
            download_ids=ids
        )
    except Exception as e:
        print(f"Analytics Error: {e}")