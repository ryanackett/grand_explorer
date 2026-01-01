from django.template.defaulttags import register

@register.filter
def get_dictionary_items(dictionary, key):

    return dictionary.get(key)