from blitzdb.queryset import QuerySet
from blitzdb.backends.file.store import TransactionalCompressedStore,TransactionalStore,Store
from blitzdb.backends.file.index import TransactionalIndex,Index
from blitzdb.backends.file.utils import JsonEncoder
from blitzdb.backends.base import Backend as BaseBackend

import os
import os.path

import json
import gzip
import hashlib
import datetime
import uuid
import copy

from collections import defaultdict

class Backend(BaseBackend):

    """
    A backend stores and retrieves objects in files.
    """

    #The default store & index classes that we will use
    CollectionStore = TransactionalStore
    Index = TransactionalIndex
    IndexStore = Store

    def __init__(self,path,autocommit = False):
        super(Backend,self).__init__()

        self._path = os.path.abspath(path)
        if not os.path.exists(path):
            os.makedirs(path)

        self.collections = {}
        self.stores = {}
        self.autocommit = autocommit
        self.indexes = defaultdict(lambda : {})
        self.index_stores = defaultdict(lambda : {})
        self.load_config()
        self.in_transaction = False
        self.begin()

    def load_config(self):
        config_file = self._path+"/config.json"
        if os.path.exists(config_file):
            with open(config_file,"rb") as config_file:
                self._config = json.loads(config_file.read())
        else:
            self._config = {
                'indexes' : {}
            }
            self.save_config()

    def save_config(self):
        config_file = self._path+"/config.json"
        with open(config_file,"wb") as config_file:
            config_file.write(json.dumps(self._config))
        
    @property
    def path(self):
        return self._path

    def get_collection_store(self,collection):
        if not collection in self.stores:
            self.stores[collection] = self.CollectionStore({'path':self.path+"/"+collection+"/objects"})
        return self.stores[collection]

    def get_index_store(self,collection,store_key):
        if not store_key in self.index_stores[collection]:
            self.index_stores[collection][store_key] = self.IndexStore({'path':self.path+"/"+collection+"/indexes/"+store_key})
        return self.index_stores[collection][store_key]

    def register(self,cls,parameters):
        super(Backend,self).register(cls,parameters)
        self.init_indexes(self.get_collection_for_cls(cls))

    def begin(self):
        """
        Starts a new transaction
        """
        if self.in_transaction:#we're already in a transaction...
            self.commit()
        self.in_transaction = True
        for collection,store in self.stores.items():
            store.begin()
            indexes = self.indexes[collection]
            for index in indexes.values():
                index.begin()

    def rollback(self):
        """
        Rolls back a transaction
        """
        if not self.in_transaction:
            raise Exception("Not in a transaction!")
        for collection,store in self.stores.items():
            store.rollback()
            indexes = self.indexes[collection]
            for index in indexes.values():
                index.rollback()
        self.in_transaction = False

    def commit(self):
        """
        Commits a transaction
        """
        for collection in self.collections:
            store = self.get_collection_store(collection)
            indexes = self.get_collection_indexes(collection)
            store.commit()
            for index in indexes.values():
                index.commit()
        self.in_transaction = False
        self.begin()

    def init_indexes(self,collection):
        print collection
        if collection in self._config['indexes']:
            print self._config['indexes'][collection]

            #If not pk index is present, we create one on the fly...
            if not [idx for idx in self._config['indexes'][collection].values() if idx['key'] == 'pk']:
                self.create_index(collection,{'key':'pk'})
            
            #We sort the indexes such that pk is always created first...
            for index_params in sorted(self._config['indexes'][collection].values(),key = lambda x: 0 if x['key'] == 'pk' else 1):
                index = self.create_index(collection,index_params)
        else:
            #If no indexes are given, we just create a primary key index...
            self.create_index(collection,{'key':'pk'})

        
    def rebuild_index(self,collection,key):
        index = self.indexes[collection][key]
        all_objects = self.filter(collection,{})
        for obj in all_objects:
            serialized_attributes = self.serialize(obj.attributes)#optimize this!
            index.add_key(serialized_attributes,obj._store_key)
        if self.autocommit:
            self.commit()

    def create_index(self,cls_or_collection,params):
        if not isinstance(params,dict):
            params = {'key' : params}
        if not isinstance(cls_or_collection,str) and not isinstance(cls_or_collection,unicode):
            collection = self.get_collection_for_cls(cls_or_collection)
        else:
            collection = cls_or_collection
        if params['key'] in self.indexes[collection]:
            return #Index already exists
        if not 'id' in params:
            params['id'] = uuid.uuid4().hex 

        index_store = self.get_index_store(collection,params['id'])
        index = self.Index(params,index_store)
        self.indexes[collection][params['key']] = index

        if not collection in self._config['indexes']:
            self._config['indexes'][collection] = {}

        self._config['indexes'][collection][params['key']] = params
        self.save_config()

        if not index.loaded:#If the index failed to load, we rebuild it...
            self.rebuild_index(collection,index.key)

        return index

    def get_collection_indexes(self,collection):
        return self.indexes[collection] if collection in self.indexes else {}

    def encode_attributes(self,attributes):
        return json.dumps(attributes,cls = JsonEncoder)

    def decode_attributes(self,data):
        return json.loads(data)

    def get_object(self,cls,key):
        collection = self.get_collection_for_cls(cls)
        store = self.get_collection_store(collection)
        try:
            data = self.deserialize(self.decode_attributes(store.get_blob(key)))
        except IOError:
            raise AttributeError("Object does not exist!")
        obj = self.create_instance(cls,data)
        return obj

    def save(self,obj):
        collection = self.get_collection_for_obj(obj)
        indexes = self.get_collection_indexes(collection)
        store = self.get_collection_store(collection)

        if obj.pk == None:
            obj.pk = uuid.uuid4().hex 

        serialized_attributes = self.serialize(obj.attributes)
        data = self.encode_attributes(serialized_attributes)
    
        try:
            store_key = self.get_pk_index(collection).get_keys_for(obj.pk).pop()
        except KeyError:
            store_key = uuid.uuid4().hex
    
        store.store_blob(data,store_key)

        for key,index in indexes.items():
            index.add_key(serialized_attributes,store_key)

        if self.autocommit:
            self.commit()

        return obj

    def get_pk_index(self,collection):
        return self.indexes[collection]['pk']

    def delete(self,obj):
        
        collection = self.get_collection_for_obj(obj)
        store = self.get_collection_store(collection)
        indexes = self.get_collection_indexes(collection)
        
        store_keys = self.get_pk_index(collection).get_keys_for(obj.pk)
        
        for store_key in store_keys:
            try:
                store.delete_blob(store_key)
            except IOError:
                pass
            for index in indexes.values():
                index.remove_key(store_key)

        if self.autocommit:
            self.commit()

    def get(self,cls,query):
        objects = self.filter(cls,query,limit = 1)
        if len(objects) == 0 or len(objects) > 1:
            raise AttributeError
        return objects[0]
        
    def filter(self,cls_or_collection,query,sort_by = None,limit = None,offset = None):

        if not isinstance(query,dict):
            raise AttributeError("Query parameters must be dict!")

        if not isinstance(cls_or_collection,str) and not isinstance(cls_or_collection,unicode):
            collection = self.get_collection_for_cls(cls_or_collection)
            cls = cls_or_collection
        else:
            collection = cls_or_collection
            cls = self.get_cls_for_collection(collection)

        store = self.get_collection_store(collection)
        indexes = self.get_collection_indexes(collection)
        compiled_query = self.compile_query(query)

        unindexed_queries = []
        indexed_queries = []

        indexes_by_key = dict([(idx.key,idx) for idx in indexes.values()])

        for key,accessor,value in compiled_query:
            if key in indexes_by_key:
                indexed_queries.append([indexes_by_key[key],value])
            else:
                unindexed_queries.append([accessor,value])

        if indexed_queries:
            keys = None
            for index,value in indexed_queries:
                if not keys:
                    keys = index.get_keys_for(value)
                else:
                    keys &= index.get_keys_for(value)
        else:
            #We fetch ALL keys from the primary index. If it is not present we're in trouble!
            keys = self.get_pk_index(collection).get_all_keys()

        for accessor,value in unindexed_queries:
            keys_to_remove = set()
            for key in keys:
                try:
                    attributes = self.decode_attributes(store.get_blob(key))
                except IOError:
                    raise Exception("Index is corrupt!")
                try:
                    if callable(value):
                        if not value(accessor(attributes)):
                            keys_to_remove.add(key)
                    else:
                        accessed_value = accessor(attributes)
                        if isinstance(accessed_value,list):
                            if value not in accessed_value: 
                                keys_to_remove.add(key)
                        elif accessed_value != value:
                            keys_to_remove.add(key) 
                except (KeyError,IndexError):
                    keys_to_remove.add(key)
            keys -= keys_to_remove

        return QuerySet(self,store,cls,keys)

