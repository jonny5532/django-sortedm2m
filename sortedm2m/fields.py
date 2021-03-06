# -*- coding: utf-8 -*-
from django.db import router
from django.db.models import signals
from django.db.models.fields.related import add_lazy_relation, create_many_related_manager
from django.db.models.fields.related import ManyToManyField, ReverseManyRelatedObjectsDescriptor
from django.db.models.fields.related import RECURSIVE_RELATIONSHIP_CONSTANT
from django.utils.functional import curry
from sortedm2m.forms import SortedMultipleChoiceField


SORT_VALUE_FIELD_NAME = 'sort_value'


def create_sorted_many_to_many_intermediate_model(field, klass):
    from django.db import models
    managed = True
    if isinstance(field.rel.to, basestring) and field.rel.to != RECURSIVE_RELATIONSHIP_CONSTANT:
        to_model = field.rel.to
        to = to_model.split('.')[-1]
        def set_managed(field, model, cls):
            field.rel.through._meta.managed = model._meta.managed or cls._meta.managed
        add_lazy_relation(klass, field, to_model, set_managed)
    elif isinstance(field.rel.to, basestring):
        to = klass._meta.object_name
        to_model = klass
        managed = klass._meta.managed
    else:
        to = field.rel.to._meta.object_name
        to_model = field.rel.to
        managed = klass._meta.managed or to_model._meta.managed
    name = '%s_%s' % (klass._meta.object_name, field.name)
    if field.rel.to == RECURSIVE_RELATIONSHIP_CONSTANT or to == klass._meta.object_name:
        from_ = 'from_%s' % to.lower()
        to = 'to_%s' % to.lower()
    else:
        from_ = klass._meta.object_name.lower()
        to = to.lower()
    meta = type('Meta', (object,), {
        'db_table': field._get_m2m_db_table(klass._meta),
        'managed': managed,
        'auto_created': klass,
        'app_label': klass._meta.app_label,
        'unique_together': (from_, to),
        'ordering': (SORT_VALUE_FIELD_NAME,),
        'verbose_name': '%(from)s-%(to)s relationship' % {'from': from_, 'to': to},
        'verbose_name_plural': '%(from)s-%(to)s relationships' % {'from': from_, 'to': to},
    })
    # Construct and return the new class.
    def default_sort_value(name):
        model = models.get_model(klass._meta.app_label, name)
        return model._default_manager.count()

    default_sort_value = curry(default_sort_value, name)

    return type(name, (models.Model,), {
        'Meta': meta,
        '__module__': klass.__module__,
        from_: models.ForeignKey(klass, related_name='%s+' % name),
        to: models.ForeignKey(to_model, related_name='%s+' % name),
        SORT_VALUE_FIELD_NAME: models.IntegerField(default=default_sort_value),
        '_sort_field_name': SORT_VALUE_FIELD_NAME,
        '_from_field_name': from_,
        '_to_field_name': to,
    })


def create_sorted_many_related_manager(superclass, rel):
    RelatedManager = create_many_related_manager(superclass, rel)

    class SortedRelatedManager(RelatedManager):
        def get_query_set(self):
            # We use ``extra`` method here because we have no other access to
            # the extra sorting field of the intermediary model. The fields
            # are hidden for joins because we set ``auto_created`` on the
            # intermediary's meta options.
            return super(SortedRelatedManager, self).\
                get_query_set().\
                extra(order_by=['%s.%s' % (
                    rel.through._meta.db_table,
                    rel.through._sort_field_name,
                )])

        def _add_items(self, source_field_name, target_field_name, *objs):
            # join_table: name of the m2m link table
            # source_field_name: the PK fieldname in join_table for the source object
            # target_field_name: the PK fieldname in join_table for the target object
            # *objs - objects to add. Either object instances, or primary keys of object instances.

            # If there aren't any objects, there is nothing to do.
            from django.db.models import Model
            if objs:
                new_ids = []
                for obj in objs:
                    if isinstance(obj, self.model):
                        if not router.allow_relation(obj, self.instance):
                           raise ValueError('Cannot add "%r": instance is on database "%s", value is on database "%s"' %
                                               (obj, self.instance._state.db, obj._state.db))
                        new_ids.append(obj.pk)
                    elif isinstance(obj, Model):
                        raise TypeError("'%s' instance expected" % self.model._meta.object_name)
                    else:
                        new_ids.append(obj)
                db = router.db_for_write(self.through.__class__, instance=self.instance)
                vals = self.through._default_manager.using(db).values_list(target_field_name, flat=True)
                vals = vals.filter(**{
                    source_field_name: self._pk_val,
                    '%s__in' % target_field_name: new_ids,
                })
                for val in vals:
                    if val in new_ids:
                        new_ids.remove(val)
                _new_ids = []
                for pk in new_ids:
                    if pk not in _new_ids:
                        _new_ids.append(pk)
                new_ids = _new_ids
                new_ids_set = set(new_ids)
                if self.reverse or source_field_name == self.source_field_name:
                    # Don't send the signal when we are inserting the
                    # duplicate data row for symmetrical reverse entries.
                    signals.m2m_changed.send(sender=rel.through, action='pre_add',
                        instance=self.instance, reverse=self.reverse,
                        model=self.model, pk_set=new_ids_set)
                # Add the ones that aren't there already
                sort_field_name = self.through._sort_field_name
                sort_field = self.through._meta.get_field_by_name(sort_field_name)[0]
                for obj_id in new_ids:
                    self.through._default_manager.using(db).create(**{
                        '%s_id' % source_field_name: self._pk_val,
                        '%s_id' % target_field_name: obj_id,
                        sort_field_name: sort_field.get_default(),
                    })
                if self.reverse or source_field_name == self.source_field_name:
                    # Don't send the signal when we are inserting the
                    # duplicate data row for symmetrical reverse entries.
                    signals.m2m_changed.send(sender=rel.through, action='post_add',
                        instance=self.instance, reverse=self.reverse,
                        model=self.model, pk_set=new_ids_set)

    return SortedRelatedManager


class ReverseSortedManyRelatedObjectsDescriptor(ReverseManyRelatedObjectsDescriptor):
    def __get__(self, instance, instance_type=None):
        if instance is None:
            return self

        # Dynamically create a class that subclasses the related
        # model's default manager.
        rel_model=self.field.rel.to
        superclass = rel_model._default_manager.__class__
        RelatedManager = create_sorted_many_related_manager(superclass, self.field.rel)

        manager = RelatedManager(
            model=rel_model,
            core_filters={'%s__pk' % self.field.related_query_name(): instance._get_pk_val()},
            instance=instance,
            symmetrical=(self.field.rel.symmetrical and isinstance(instance, rel_model)),
            source_field_name=self.field.m2m_field_name(),
            target_field_name=self.field.m2m_reverse_field_name(),
            reverse=False
        )

        return manager

    def __set__(self, instance, value):
        if instance is None:
            raise AttributeError, "Manager must be accessed via instance"

        manager = self.__get__(instance)
        manager.clear()
        manager.add(*value)


class SortedManyToManyField(ManyToManyField):
    '''
    Providing a many to many relation that remembers the order of related
    objects.

    Accept a boolean ``sorted`` attribute which specifies if relation is
    ordered or not. Default is set to ``True``. If ``sorted`` is set to
    ``False`` the field will behave exactly like django's ``ManyToManyField``.
    '''
    def __init__(self, to, sorted=True, **kwargs):
        self.sorted = sorted
        super(SortedManyToManyField, self).__init__(to, **kwargs)
        if self.sorted:
            self.help_text = kwargs.get('help_text', None)

    def contribute_to_class(self, cls, name):
        if not self.sorted:
            return super(SortedManyToManyField, self).contribute_to_class(cls, name)

        # To support multiple relations to self, it's useful to have a non-None
        # related name on symmetrical relations for internal reasons. The
        # concept doesn't make a lot of sense externally ("you want me to
        # specify *what* on my non-reversible relation?!"), so we set it up
        # automatically. The funky name reduces the chance of an accidental
        # clash.
        if self.rel.symmetrical and (self.rel.to == "self" or self.rel.to == cls._meta.object_name):
            self.rel.related_name = "%s_rel_+" % name

        super(ManyToManyField, self).contribute_to_class(cls, name)

        # The intermediate m2m model is not auto created if:
        #  1) There is a manually specified intermediate, or
        #  2) The class owning the m2m field is abstract.
        if not self.rel.through and not cls._meta.abstract:
            self.rel.through = create_sorted_many_to_many_intermediate_model(self, cls)

        # Add the descriptor for the m2m relation
        setattr(cls, self.name, ReverseSortedManyRelatedObjectsDescriptor(self))

        # Set up the accessor for the m2m table name for the relation
        self.m2m_db_table = curry(self._get_m2m_db_table, cls._meta)

        # Populate some necessary rel arguments so that cross-app relations
        # work correctly.
        if isinstance(self.rel.through, basestring):
            def resolve_through_model(field, model, cls):
                field.rel.through = model
            add_lazy_relation(cls, self, self.rel.through, resolve_through_model)

        if isinstance(self.rel.to, basestring):
            target = self.rel.to
        else:
            target = self.rel.to._meta.db_table
        cls._meta.duplicate_targets[self.column] = (target, "m2m")

    def formfield(self, **kwargs):
        defaults = {}
        if self.sorted:
            defaults['form_class'] = SortedMultipleChoiceField
        defaults.update(kwargs)
        return super(SortedManyToManyField, self).formfield(**defaults)

# Add introspection rules for South database migrations
# See http://south.aeracode.org/docs/customfields.html
try:
	from south.modelsinspector import add_introspection_rules
	add_introspection_rules(
        [(
            (SortedManyToManyField,),
            [],
            {"sorted": ["sorted", {"default": True}]},
        )],
        [r'^sortedm2m\.fields\.SortedManyToManyField']
    )
except ImportError:
	pass
