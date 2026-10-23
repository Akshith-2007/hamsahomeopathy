from django import forms


class BootstrapMixin:
    """Apply Bootstrap 5 classes to every widget; optionally render read-only."""

    textarea_rows = 2
    # Sections live in collapsible accordions; browser-side `required` would block
    # submitting a hidden field, so validation is done server-side only.
    use_required_attribute = False

    def __init__(self, *args, read_only=False, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput,)):
                css = "form-check-input"
            elif isinstance(widget, forms.RadioSelect):
                css = "form-check-input"
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                css = "form-select form-select-sm"
            else:
                css = "form-control form-control-sm"
            if isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("rows", self.textarea_rows)
            widget.attrs["class"] = f"{widget.attrs.get('class', '')} {css}".strip()
            if read_only:
                field.disabled = True
