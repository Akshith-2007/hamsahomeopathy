from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    return mapping.get(key) if mapping else None


@register.filter
def field_pairs(form, names):
    """Return bound fields for a comma-separated list of names."""
    return [form[name.strip()] for name in names.split(",")]


@register.simple_tag
def section_state(open_sections, name):
    return "show" if name in open_sections else ""


@register.filter
def initials(name):
    parts = [p for p in str(name).split() if p]
    return "".join(p[0] for p in parts[:2]).upper()


@register.simple_tag(takes_context=True)
def url_replace(context, **kwargs):
    """Current query string with the given keys replaced (used for pagination)."""
    query = context["request"].GET.copy()
    for key, value in kwargs.items():
        query[key] = value
    return query.urlencode()


@register.filter
def bound(form, name):
    """{{ form|bound:"field_name" }} -> the BoundField."""
    return form[name]


@register.filter
def dict_items(value):
    """Safe `.items()` for templates: a key named "items" would shadow the method in `x.items`."""
    return list(value.items()) if isinstance(value, dict) else []


@register.filter
def is_dict(value):
    return isinstance(value, dict)
