from django.template.defaulttags import register

@register.filter
def get_dictionary_items(dictionary, key):
    print("below")
    print(dictionary)
    print(key)
    print(dictionary.get(key))
    return dictionary.get(key)