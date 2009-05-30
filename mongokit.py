# -*- coding: utf-8 -*-
#
# Copyright (c) 2009 Nicolas Clairon
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
#
__author__ = 'n.namlook {at} gmail {dot} com'

import datetime
import pymongo
from pymongo.connection import Connection
from exceptions import *
import re

from uuid import uuid4

authorized_types = [type(None), bool, int, float, unicode, list, dict,
  datetime.datetime, 
  pymongo.binary.Binary,
  pymongo.objectid.ObjectId,
  pymongo.dbref.DBRef,
  pymongo.code.Code,
  type(re.compile("")),
]

class MongoDocument(dict):
    """
    A dictionnary with a building structured schema
    The validate method will check that the document
    match the underling structure

    The structure take the followin form:

        structure = {
            "key1":{
                "foo":int,
                "bar:{unicode:int}
            },
            "key2":{
                "spam":unicode,
                "eggs":[int]
            },
            "bla":float
        }

    authorized_types are listed in `mongokit.authorized_types`
    
    We can describe fields as required with the required attribute:

        required = ["keys1.foo", "bla"]

    = Default values =

    Default values can be set by using the attribute default_values :

        default_values = {"key1.foo":3}

    = Validators =

    Validators can be added in order to validate some values :

        validators = {
            "key1.foo":lambda x: x>5,
            "key2.spam": lambda x: x.startswith("miam")
        }

    You can set multiple validators :

        validators = {
            "key1.foo": [validator1, validator2]
        }

    A MongoDocument works just like dict:

        >>> my_doc = MongoDocument()
        >>> my_doc['key1']['foo'] = 42
        >>> my_doc['bla'] = 7.0
    
    Validation is made with the `validate()` methode:
        
        >>> my_doc.validate()
        >>> my_doc['key2']['spam'] = 2
        >>> my_doc.validate()
        <type 'exceptions.AssertionError'>: spam : 2 must not be int...
        >>> del my_doc["bla"]
        >>> my_doc.validate()
        <type 'exceptions.ValueError'>: bla is required

    = Signals =

    Signals can be mapped to a field. Each time a field will changed, the function
    will be called. A signal is called before field validation so you can make some
    field processing:
        
        signals = {
            "key1.foo": lambda doc, value: doc['bla'] = unicode(value)
        }

    This means that each time key1.foo will be changed, the value of field "bla" will
    change to. You can make more complicated signals. A signals return nothing.

    Juste like validators, you can specify multiple signals for one field.

    == validate keys ==

    If the value of key is not known but we want to validate some deeper structure, 
    we use the "$<type>" descriptor :

        class MyDoc(MongoDocument):
            structure = {
                "key1":{
                    unicode:{
                        "bla":int,
                        "bar:{unicode:int}
                    }
                }
                "bla":float
            }
            required = ["key1.$unicode.bla"]

    Not that if you use python type as key in structure, generate_skeleton
    won't be able to build the entired underline structure :

        >>> MyDoc()
        {'bla': None, 'key1': {}}

    So, default_values nor signals will work.
    """
    
    auto_inheritance = True
    structure = None
    required_fields = []
    default_values = {}
    validators = {}
    signals = {}

    db_host = "localhost"
    db_port = 27017
    db_name = None
    collection_name = None

    _collection = None
    
    def __init__(self, doc={}, gen_skel=True, auto_inheritance=True):
        """
        doc : a document dictionnary
        gen_skel : if True, generate automaticly the skeleton of the doc
            filled with NoneType each time validate() is called. Note that
            if doc is not {}, gen_skel is always False. If gen_skel is False,
            default_values cannot be filled.
        auto_inheritance: enable the automatic inheritance (default)
        """
        #
        # inheritance
        #
        if self.auto_inheritance and auto_inheritance:
            self.generate_inheritance()
        # init
        if self.structure is None:
            raise StructureError("your document must have a structure defined")
        self._validate_structure()
        self._namespaces = list(self.__walk_dict(self.structure))
        self.__validate_descriptors()
        self.__signals = {}
        for k,v in doc.iteritems():
            self[k] = v
        if doc:
            gen_skel = False
        if gen_skel:
            self.generate_skeleton()
            self._set_default_fields(self, self.structure)
        self._process_signals(self, self.structure)
        self._collection = None
        ## building required fields namespace
        self._required_namespace = set([])
        for rf in self.required_fields:
            splited_rf = rf.split('.')
            for index in range(len(splited_rf)):
                self._required_namespace.add(".".join(splited_rf[:index+1]))
     
    def __walk_dict(self, dic):
        # thanks jean_b for the patch
        for key, value in dic.items():
            if isinstance(value, dict) and len(value):
                yield key
                for child_key in self.__walk_dict(value):
                    if type(key) is type:
                        new_key = "$%s" % key.__name__
                    else:
                        new_key = key
                    if type(child_key) is type:
                        new_child_key = "$%s" % child_key.__name__
                    else:
                        new_child_key = child_key
                    yield '%s.%s' % (new_key, new_child_key)
            elif type(key) is type:
                yield '$%s' % key.__name__
            else:
                if type(key) is not type:
                    yield key
                else:
                    yield ""

    def generate_skeleton(self):
        """
        validate and generate the skeleton of the document
        from the structure (unknown values are set to None)
        """
        self.__generate_skeleton(self, self.structure)

    def generate_inheritance(self):
        """
        generate self.structure, self.validators, self.default_values
        and self.signals from ancestors
        """
        parent = self.__class__.__mro__[1]
        if hasattr(parent, "structure") and parent is not MongoDocument:
            parent = parent()
            if parent.structure:
                self.structure.update(parent.structure)
            if parent.required_fields:
                self.required_fields = list(set(self.required_fields+parent.required_fields))
            if parent.default_values:
                obj_default_values = self.default_values.copy()
                self.default_values = parent.default_values.copy()
                self.default_values.update(obj_default_values)
            if parent.validators:
                obj_validators = self.validators.copy()
                self.validators = parent.validators.copy()
                self.validators.update(obj_validators)
            if parent.signals:
                obj_signals = self.signals.copy()
                self.signals = parent.signals.copy()
                self.signals.update(obj_signals)

    def __validate_descriptors(self):
        for dv in self.default_values:
            if dv not in self._namespaces:
                raise ValueError("Error in default_values: can't find %s in structure" % dv )
        for signal in self.signals:
            if signal not in self._namespaces:
                raise ValueError("Error in signals: can't find %s in structure" % signal )
        for validator in self.validators:
            if validator not in self._namespaces:
                raise ValueError("Error in validators: can't find %s in structure" % validator )

    def _validate_structure(self):
        ##############
        def __validate_structure( struct):
            if type(struct) is type:
                if struct not in authorized_types:
                    raise StructureError("%s is not an authorized_types" % key)
            elif isinstance(struct, dict):
                for key in struct:
                    if isinstance(key, basestring):
                        if "." in key: raise BadKeyError("%s must not contain '.'" % key)
                        if key.startswith('$'): raise BadKeyError("%s must not start with '$'" % key)
                    elif type(key) is type:
                        if not key in authorized_types:
                            raise AuthorizedTypeError("%s is not an authorized type" % key)
                    else:
                        raise StructureError("%s must be a basestring or a type" % key)
                    if isinstance(struct[key], dict):
                        __validate_structure(struct[key])
                    elif isinstance(struct[key], list):
                        __validate_structure(struct[key])
                    elif struct[key] not in authorized_types:
                        raise StructureError("%s is not an authorized type" % struct[key])
            elif isinstance(struct, list):
                for item in struct:
                    __validate_structure(item)
        #################
        if self.structure is None:
            raise StructureError("self.structure must not be None")
        if not isinstance(self.structure, dict):
            raise StructureError("self.structure must be a dict instance")
        if self.required_fields:
            if len(self.required_fields) != len(set(self.required_fields)):
                raise DuplicateRequiredError("duplicate required_fields : %s" % self.required_fields)
        __validate_structure(self.structure)
                    
    def _validate_doc(self, doc, struct, path = ""):
        if type(struct) is type or struct is None:
            if struct is None:
                if type(doc) not in authorized_types:
                    raise AuthorizedTypeError("%s is not an authorized types" % type(doc).__name__)
            elif not isinstance(doc, struct) and doc is not None:
                raise SchemaTypeError("%s must be an instance of %s not %s" % (path, struct.__name__, type(doc).__name__))
        elif isinstance(struct, dict):
            if not isinstance(doc, type(struct)):
                raise SchemaTypeError("%s must be an instance of %s not %s" %(path, type(struct).__name__, type(doc).__name__))
            if len(doc) != len(struct):
                struct_doc_diff = list(set(struct).difference(set(doc)))
                if struct_doc_diff:
                    for field in struct_doc_diff:
                        if type(field) is not type:
                            raise StructureError( "missed fields : %s" % struct_doc_diff )
                else:
                    struct_struct_diff = list(set(doc).difference(set(struct)))
                    if struct_struct_diff != ['_id']:
                        raise StructureError( "unknown fields : %s" % struct_struct_diff)
            for key in struct:
                if type(key) is type:
                    new_key = "$%s" % key.__name__
                else:
                    new_key = key
                new_path = ".".join([path, new_key]).strip('.')
                if new_key.split('.')[-1].startswith("$"):
                    for doc_key in doc:
                        if not isinstance(doc_key, key):
                            raise SchemaTypeError("key of %s must be an instance of %s not %s" % (path, key.__name__, type(doc_key).__name__))
                        self._validate_doc(doc[doc_key], struct[key], new_path)
                else:
                    self._validate_doc(doc[key], struct[key],  new_path)
        elif isinstance(struct, list):
            if not isinstance(doc, list):
                raise SchemaTypeError("%s must be an instance of list not %s" % (path, type(doc).__name__))
            if not len(struct):
                struct = None
            else:
                struct = struct[0]
            for obj in doc:
                self._validate_doc(obj, struct, path)

    def _process_validators(self, doc, struct, path = ""):
        #################################################
        def __processval( self, new_path, doc, key ):
                #
                # check that the value pass througt the validator process
                #
                if new_path in self.validators and doc[key] is not None:
                    if not hasattr(self.validators[new_path], "__iter__"):
                        validators = [self.validators[new_path]]
                    else:
                        validators = self.validators[new_path]
                    for validator in validators:
                        if not validator(doc[key]):
                            raise ValidationError("%s does not pass the validator %s" % (new_path, validator.__name__))
        #################################################
        for key in struct:
            if type(key) is type:
                new_key = "$%s" % key.__name__
            else:
                new_key = key
            new_path = ".".join([path, new_key]).strip('.')
            #
            # if the value is a dict, we have a another structure to validate
            #
            if isinstance(struct[key], dict):
                #
                # if the dict is still empty into the document we build it with None values
                #
                if type(key) is not type and key not in doc:
                    __processval(self, new_path, doc)
                elif type(key) is type:
                    for doc_key in doc:
                        self._process_validators(doc[doc_key], struct[key], new_path)
                        #self._process_validators(doc[key], struct[key], new_path)
                else:
                    self._process_validators(doc[key], struct[key], new_path)
            #
            # If the struct is a list, we have to validate all values into it
            #
            elif type(struct[key]) is list:
                #
                # check if the list must not be null
                #
                if not key in doc:
                    __processval(self, new_path, doc, key)
                elif not len(doc[key]):
                    __processval(self, new_path, doc, key)
            #
            # It is not a dict nor a list but a simple key:value
            #
            else:
                #
                # check if the value must not be null
                #
                __processval(self, new_path, doc, key)
            
    def _set_default_fields(self, doc, struct, path = ""):
        for key in struct:
            if type(key) is type:
                new_key = "$%s" % key.__name__
            else:
                new_key = key
            new_path = ".".join([path, new_key]).strip('.')
            #
            # default_values :
            # if the value is None, check if a default value exist.
            # if exists, and it is a function then call it otherwise, juste feed it
            #
            if doc[key] is None and new_path in self.default_values:
                new_value = self.default_values[new_path]
                if callable(new_value):
                    doc[key] = new_value()
                else:
                    doc[key] = new_value
            #
            # if the value is a dict, we have a another structure to validate
            #
            if isinstance(struct[key], dict):
                #
                # if the dict is still empty into the document we build it with None values
                #
                if len(struct[key]) and not [i for i in struct[key].keys() if type(i) is type]:
                    self._set_default_fields(doc[key], struct[key], new_path)
                else:
                    if new_path in self.default_values:
                        new_value = self.default_values[new_path]
                        if callable(new_value):
                            doc[key] = new_value()
                        else:
                            doc[key] = new_value
            else: # list or what else
                if new_path in self.default_values:
                    new_value = self.default_values[new_path]
                    if callable(new_value):
                        doc[key] = new_value()
                    else:
                        doc[key] = new_value

    def _process_signals(self, doc, struct, path = ""):
        #################################################
        def __procsignals(self, new_path, doc):
            if new_path in self.signals:
                launch_signals = True
            else:
                launch_signals = False
            if new_path in self.signals and launch_signals:
                make_signal = False
                if new_path in self.__signals:
                    if doc[key] != self.__signals[new_path]:
                        make_signal = True
                else:
                    make_signal = True
                if make_signal:
                    if not hasattr(self.signals[new_path], "__iter__"):
                        signals = [self.signals[new_path]]
                    else:
                        signals = self.signals[new_path]
                    for signal in signals:
                        signal(self, doc[key])
                    self.__signals[new_path] = doc[key]
        ##################################################
        for key in struct:
            if type(key) is type:
                new_key = "$%s" % key.__name__
            else:
                new_key = key
            new_path = ".".join([path, new_key]).strip('.')
            #
            # if the value is a dict, we have a another structure to validate
            #
            if isinstance(struct[key], dict):
                #
                # if the dict is still empty into the document we build it with None values
                #
                if key in doc:
                    self._process_signals(doc[key], struct[key], new_path)
                else:
                    pass
                    # TODO signals_namespace
            #
            # It is not a dict nor a list but a simple key:value
            #
            else:
                #
                # check if the value must not be null
                #
                if new_path in self.signals:
                    __procsignals(self, new_path, doc)

    def _validate_required(self, doc, struct, path = ""):
        for key in struct:
            if type(key) is type:
                new_key = "$%s" % key.__name__
            else:
                new_key = key
            new_path = ".".join([path, new_key]).strip('.')
            #
            # if the value is a dict, we have a another structure to validate
            #
            if isinstance(struct[key], dict):
                #
                # if the dict is still empty into the document we build it with None values
                #
                if type(key) is not type and key not in doc:
                    if new_path in self._required_namespace:
                        raise RequireFieldError("%s is required" % new_path)
                elif type(key) is type:
                    if not len(doc):
                        if new_path in self._required_namespace:
                            raise RequireFieldError("%s is required" % new_path)
                    else:
                        for doc_key in doc:
                            self._validate_required(doc[doc_key], struct[key], new_path)
                elif not len(doc[key]) and new_path in self._required_namespace:
                    raise RequireFieldError( "%s is required" % new_path )
                else:
                    self._validate_required(doc[key], struct[key], new_path)
            #
            # If the struct is a list, we have to validate all values into it
            #
            elif type(struct[key]) is list:
                #
                # check if the list must not be null
                #
                if not key in doc:
                    if new_path in self._required_namespace:
                        raise RequireFieldError( "%s is required" % new_path )
                elif not len(doc[key]) and new_path in self.required_fields:
                    raise RequireFieldError( "%s is required" % new_path )
            #
            # It is not a dict nor a list but a simple key:value
            #
            else:
                #
                # check if the value must not be null
                #
                if not key in doc:
                    if new_path in self._required_namespace:
                        raise RequireFieldError( "%s is required" % new_path )
                elif doc[key] is None and new_path in self._required_namespace:
                    raise RequireFieldError( "%s is required" % new_path )


    def __generate_skeleton(self, doc, struct, path = ""):
        for key in struct:
            #
            # Automatique generate the skeleton with NoneType
            #
            if type(key) is not type and key not in doc:
                if isinstance(struct[key], dict):
                    doc[key] = type(struct[key])()
                elif struct[key] is dict:
                    doc[key] = {}
                elif isinstance(struct[key], list):
                    doc[key] = type(struct[key])()
                elif struct[key] is list:
                    doc[key] = []
                else:
                    doc[key] = None
            #
            # if the value is a dict, we have a another structure to validate
            #
            if isinstance(struct[key], dict) and type(key) is not type:
                self.__generate_skeleton(doc[key], struct[key], path)

    def validate(self):
        self._process_signals(self, self.structure)
        self._validate_doc(self, self.structure)
        self._validate_required(self, self.structure)
        self._process_validators(self, self.structure)

    def save(self, uuid=True, validate=True, safe=True, *args, **kwargs):
        if validate:
            self.validate()
        if '_id' not in self and uuid:
            self['_id'] = unicode("%s-%s" % (self.__class__.__name__, uuid4()))
        id = self.collection.save(self, safe=safe, *args, **kwargs)
        return self

    @classmethod
    def get_collection(cls):
        if not cls.db_name or not cls.collection_name:
            raise ConnectionError( "You must set a db_name and a collection_name" )
        if not cls._collection:
            cls._collection = Connection(cls.db_host, cls.db_port)[cls.db_name][cls.collection_name]
        return cls._collection

    def _get_collection(self):
        return self.__class__.get_collection()
    collection = property(_get_collection)

    def db_update(self, document, upsert=False, manipulate=False, safe=True, validate=True, reload=True):
        """
        update the object in the database.

        :Parameters:
          - `document`: a SON object specifying the fields to be changed in the
            selected document(s), or (in the case of an upsert) the document to
            be inserted.
          - `upsert` (optional): perform an upsert operation
          - `manipulate` (optional): monipulate the document before updating?
          - `safe` (optional): check that the update succeeded?
          - `validate`: validate the updated object (usefull to check if update
            values follow schema)
          - `reload`: load updated field in the doc
        """
        if not self.get('_id'):
            raise AttributeError("Your document must be saved in the database updating it")
        for modif_op in document:
            if modif_op.startswith("$") and modif_op not in ["$inc", "$set", "$push"]:
                raise ModifierOperatorError("bad modifier operator : %s" % modif_op)
        self.collection.update(spec={"_id":self['_id']}, document=document,
          upsert=upsert, manipulate=manipulate, safe=safe )
        errors = self.collection.database().error()
        if errors:
            raise MongoDbError("%s" % errors['err'])
        if validate or reload:
            updated_obj = self.get_from_id(self['_id'])
            if validate:
                self.__class__(updated_obj).validate()
            if reload:
                for k,v in updated_obj.iteritems():
                    self[k]=v

    @classmethod
    def get_from_id(cls, id):
        bson_obj = cls.get_collection().find_one({"_id":id})
        if bson_obj:
            return cls(bson_obj)

    @classmethod
    def all(cls, *args, **kwargs):
        return MongoDocumentCursor(cls.get_collection().find(*args, **kwargs), cls)

    @classmethod
    def one(cls, *args, **kwargs):
        bson_obj = cls.get_collection().find(*args, **kwargs)
        count = bson_obj.count()
        if count > 1:
            raise MultipleResultsFound("%s results found" % count)
        elif count == 1:
            return cls(list(bson_obj)[0])
    
#    def __setitem__(self, key, value):
#        dict.__setitem__(self, key, value)


class MongoDocumentCursor(object):
    def __init__(self, cursor, cls):
        self._cursor = cursor
        self._class_object = cls

    def where(self, *args, **kwargs):
        return self.__class__(self._cursor.where(*args, **kwargs), self._class_object)

    def sort(self, *args, **kwargs):
        return self.__class__(self._cursor.sort(*args, **kwargs), self._class_object)

    def limit(self, *args, **kwargs):
        return self.__class__(self._cursor.limit(*args, **kwargs), self._class_object)

    def hint(self, *args, **kwargs):
        return self.__class__(self._cursor.hint(*args, **kwargs), self._class_object)

    def count(self, *args, **kwargs):
        return self._cursor.count(*args, **kwargs)
        
    def explain(self, *args, **kwargs):
        return self._cursor.explain(*args, **kwargs)

    def next(self, *args, **kwargs):
        return self._class_object(self._cursor.next(*args, **kwargs))

    def skip(self, *args, **kwargs):
        return self.__class__(self._cursor.skip(*args, **kwargs), self._class_object)

    def __iter__(self, *args, **kwargs):
        for obj in self._cursor:
            yield self._class_object(obj)

