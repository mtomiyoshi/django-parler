from django import forms
from django.forms.forms import BoundField
from django.forms.models import ModelFormMetaclass, BaseInlineFormSet
from django.utils.functional import cached_property
from django.utils.translation import get_language
from django.utils import six
from parler.models import TranslationDoesNotExist
from parler.utils import compat


__all__ = (
    'TranslatableModelForm',
    'TranslatedField',
    'TranslatableModelFormMixin',
    #'TranslatableModelFormMetaclass',
)


class TranslatedField(object):
    """
    A wrapper for a translated form field.

    This wrapper can be used to declare translated fields on the form, e.g.

    .. code-block:: python

        class MyForm(TranslatableModelForm):
            title = TranslatedField()
            slug = TranslatedField()

            description = TranslatedField(form_class=forms.CharField, widget=TinyMCE)
    """
    def __init__(self, **kwargs):
        # The metaclass performs the magic replacement with the actual formfield.
        self.kwargs = kwargs



class TranslatableBoundField(BoundField):
    """
    Decorating the regular BoundField to distinguish translatable fields in the admin.
    """
    #: A tagging attribute, making it easy for templates to identify these fields
    is_translatable = True

    def label_tag(self, contents=None, attrs=None, label_suffix=None):
        if attrs is None:
            attrs = {}

        attrs['class'] = (attrs.get('class', '') + " translatable-field").strip()
        return super(TranslatableBoundField, self).label_tag(contents=contents, attrs=attrs, label_suffix=label_suffix)

    def as_widget(self, widget=None, attrs=None, only_initial=False):
        if attrs is None:
            attrs = {}

        attrs['class'] = (attrs.get('class', '') + " translatable-field").strip()
        return super(TranslatableBoundField, self).as_widget(widget=widget, attrs=attrs, only_initial=only_initial)




class BaseTranslatableModelForm(forms.BaseModelForm):
    """
    The base methods added to :class:`TranslatableModelForm` to fetch and store translated fields.
    """
    language_code = None    # Set by TranslatableAdmin.get_form() on the constructed subclass.


    def __init__(self, *args, **kwargs):
        current_language = kwargs.pop('_current_language', None)   # Used for TranslatableViewMixin
        super(BaseTranslatableModelForm, self).__init__(*args, **kwargs)

        # Load the initial values for the translated fields
        instance = kwargs.get('instance', None)
        if instance:
            for meta in instance._parler_meta:
                try:
                    # By not auto creating a model, any template code that reads the fields
                    # will continue to see one of the other translations.
                    # This also causes admin inlines to show the fallback title in __unicode__.
                    translation = instance._get_translated_model(meta=meta)
                except TranslationDoesNotExist:
                    pass
                else:
                    for field in meta.get_translated_fields():
                        self.initial.setdefault(field, getattr(translation, field))

        # Typically already set by admin
        if self.language_code is None:
            if instance:
                self.language_code = instance.get_current_language()
                return
            else:
                self.language_code = current_language or get_language()

    def _post_clean(self):
        # Copy the translated fields into the model
        # Make sure the language code is set as early as possible (so it's active during most clean() methods)
        self.instance.set_current_language(self.language_code)
        self.save_translated_fields()

        # Perform the regular clean checks, this also updates self.instance
        super(BaseTranslatableModelForm, self)._post_clean()

    def save_translated_fields(self):
        """
        Save all translated fields.
        """
        # Assign translated fields to the model (using the TranslatedAttribute descriptor)
        for field in self._translated_fields:
            try:
                value = self.cleaned_data[field]
            except KeyError:  # Field has a ValidationError
                continue
            setattr(self.instance, field, value)

    @cached_property
    def _translated_fields(self):
        field_names = self._meta.model._parler_meta.get_all_fields()
        return [f_name for f_name in field_names if f_name in self.fields]

    def __getitem__(self, name):
        """
        Return a :class:`TranslatableBoundField` for translated models.
        This extends the default ``form[field]`` interface that produces the BoundField for HTML templates.
        """
        boundfield = super(BaseTranslatableModelForm, self).__getitem__(name)
        if name in self._translated_fields:
            # Oh the wonders of Python :)
            boundfield.__class__ = TranslatableBoundField
        return boundfield



class TranslatableModelFormMetaclass(ModelFormMetaclass):
    """
    Meta class to add translated form fields to the form.
    """
    def __new__(mcs, name, bases, attrs):
        # Before constructing class, fetch attributes from bases list.
        form_meta = _get_mro_attribute(bases, '_meta')
        form_base_fields = _get_mro_attribute(bases, 'base_fields', {})  # set by previous class level.

        if form_meta:
            # Not declaring the base class itself, this is a subclass.

            # Read the model from the 'Meta' attribute. This even works in the admin,
            # as `modelform_factory()` includes a 'Meta' attribute.
            # The other options can be read from the base classes.
            form_new_meta = attrs.get('Meta', form_meta)
            form_model = form_new_meta.model if form_new_meta else form_meta.model

            # Detect all placeholders at this class level.
            placeholder_fields = [
                f_name for f_name, attr_value in six.iteritems(attrs) if isinstance(attr_value, TranslatedField)
            ]

            # Include the translated fields as attributes, pretend that these exist on the form.
            # This also works when assigning `form = TranslatableModelForm` in the admin,
            # since the admin always uses modelform_factory() on the form class, and therefore triggering this metaclass.
            if form_model:
                for translations_model in form_model._parler_meta.get_all_models():
                    fields = getattr(form_new_meta, 'fields', form_meta.fields)
                    exclude = getattr(form_new_meta, 'exclude', form_meta.exclude) or ()
                    widgets = getattr(form_new_meta, 'widgets', form_meta.widgets) or ()
                    formfield_callback = attrs.get('formfield_callback', None)

                    if fields == '__all__':
                        fields = None

                    for f_name in translations_model.get_translated_fields():
                        # Add translated field if not already added, and respect exclude options.
                        if f_name in placeholder_fields:
                            # The TranslatedField placeholder can be replaced directly with actual field, so do that.
                            attrs[f_name] = _get_model_form_field(translations_model, f_name, formfield_callback=formfield_callback, **attrs[f_name].kwargs)

                        # The next code holds the same logic as fields_for_model()
                        # The f.editable check happens in _get_model_form_field()
                        elif f_name not in form_base_fields \
                         and (fields is None or f_name in fields) \
                         and f_name not in exclude \
                         and not f_name in attrs:
                            # Get declared widget kwargs
                            if f_name in widgets:
                                # Not combined with declared fields (e.g. the TranslatedField placeholder)
                                kwargs = {'widget': widgets[f_name]}
                            else:
                                kwargs = {}

                            # See if this formfield was previously defined using a TranslatedField placeholder.
                            placeholder = _get_mro_attribute(bases, f_name)
                            if placeholder and isinstance(placeholder, TranslatedField):
                                kwargs.update(placeholder.kwargs)

                            # Add the form field as attribute to the class.
                            formfield = _get_model_form_field(translations_model, f_name, formfield_callback=formfield_callback, **kwargs)
                            if formfield is not None:
                                attrs[f_name] = formfield

        # Call the super class with updated `attrs` dict.
        return super(TranslatableModelFormMetaclass, mcs).__new__(mcs, name, bases, attrs)



def _get_mro_attribute(bases, name, default=None):
    for base in bases:
        try:
            return getattr(base, name)
        except AttributeError:
            continue
    return default



def _get_model_form_field(model, name, formfield_callback=None, **kwargs):
    """
    Utility to create the formfield from a model field.
    When a field is not editable, a ``None`` will be returned.
    """
    field = model._meta.get_field(name)
    if not field.editable:  # see fields_for_model() logic in Django.
        return None

    # Apply admin formfield_overrides
    if formfield_callback is None:
        formfield = field.formfield(**kwargs)
    elif not callable(formfield_callback):
        raise TypeError('formfield_callback must be a function or callable')
    else:
        formfield = formfield_callback(field, **kwargs)

    return formfield


class TranslatableModelForm(compat.with_metaclass(TranslatableModelFormMetaclass, BaseTranslatableModelForm, forms.ModelForm)):
    """
    The model form to use for translated models.
    """
    # six.with_metaclass does not handle more than 2 parent classes for django < 1.6
    # but we need all of them in django 1.7 to pass check admin.E016:
    #       "The value of 'form' must inherit from 'BaseModelForm'"
    # so we use our copied version in parler.utils.compat
    #
    # Also, the class must inherit from ModelForm,
    # or the ModelFormMetaclass will skip initialization.
    # It only adds the _meta from anything that extends ModelForm.



class TranslatableBaseInlineFormSet(BaseInlineFormSet):
    """
    The formset base for creating inlines with translatable models.
    """
    language_code = None

    def _construct_form(self, i, **kwargs):
        form = super(TranslatableBaseInlineFormSet, self)._construct_form(i, **kwargs)
        form.language_code = self.language_code   # Pass the language code for new objects!
        return form

    def save_new(self, form, commit=True):
        obj = super(TranslatableBaseInlineFormSet, self).save_new(form, commit)
        return obj
