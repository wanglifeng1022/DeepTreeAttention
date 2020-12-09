#Wrapper class for DeepTreeAttention
"""Wrap generate data, create, train and predict into a single set of class commands"""
import os
import re
import glob
import pandas as pd
import geopandas as gpd
import numpy as np
import tensorflow as tf

from tensorflow.keras.models import load_model
from tensorflow.keras import metrics
from tensorflow.keras.utils import multi_gpu_model
from sklearn.utils import class_weight

#Local Modules
from DeepTreeAttention.utils.config import parse_yaml
from DeepTreeAttention.models import Hang2020_geographic as Hang
from DeepTreeAttention.models import metadata
from  DeepTreeAttention.models import layers
from DeepTreeAttention.generators import boxes
from DeepTreeAttention.callbacks import callbacks


class AttentionModel():
    """The main class holding train, predict and evaluate methods"""

    def __init__(self, config="conf/config.yml", saved_model=None, log_dir=None):
        """
        Args:
            config: path to a config file, defaults to ../conf/config.yml
            saved_model: Optional, a previous saved AttentionModel .h5
        """
        self.config = parse_yaml(config)
        if saved_model:
            self.model = load_model(saved_model)

        #Holders
        self.testing_set = None
        self.training_set = None

        if log_dir:
            self.log_dir = log_dir
        else:
            self.log_dir = None

        #log config
        self.HSI_size = self.config["train"]["HSI"]["crop_size"]
        self.HSI_channels = self.config["train"]["HSI"]["sensor_channels"]
        self.HSI_weighted_sum = self.config["train"]["HSI"]["weighted_sum"]
        
        self.RGB_size= self.config["train"]["RGB"]["crop_size"]
        self.RGB_channels = self.config["train"]["RGB"]["sensor_channels"]
        self.RGB_weighted_sum = self.config["train"]["RGB"]["weighted_sum"]
        
        self.HSI_extend_box = self.config["train"]["HSI"]["extend_box"]
        self.classes_file = self.config["train"]["species_class_file"]
        
        try:
            if self.config["train"]["site_class_file"] is not None:
                self.sites = pd.read_csv(self.config["train"]["site_class_file"]).shape[0]
            if self.config["train"]["domain_class_file"] is not None:
                self.domains = pd.read_csv(self.config["train"]["domain_class_file"]).shape[0]
        except:
            pass
        
        try:
            self.train_shp = gpd.read_file(self.config["train"]["ground_truth_path"])
        except:
            self.train_shp = None
        
    def generate(self, HSI_sensor_path, RGB_sensor_path, elevation, heights, domain, site, species_label_dict=None, train=True, chunk_size=1000, shapefile=None, csv_file=None,label_column="label"):
        """Predict species class for each DeepForest bounding box
            Args:
                shapefile: a DeepForest shapefile (see NeonCrownMaps) with a bounding box and utm projection
                train: generate a training record that yields, image, label, or a prediction record with metadata? Default True
                site: site metadata label in numeric
                height: list of heights in the shapefile
                sensor_path: supply a known path to a sensor geoTIFF tile. 
                chunk_size: number of crops per tfrecord
                label_column: name of column to take taxonID labels
            """
        #set savedir
        if train:
            savedir = self.config["train"]["tfrecords"]
        else:
            savedir = self.config["predict"]["tfrecords"]

        self.classes = pd.read_csv(self.classes_file).shape[0]        
        created_records = boxes.generate_tfrecords(shapefile=shapefile,
                                                   csv_file=csv_file,
                                                   HSI_sensor_path=HSI_sensor_path,
                                                   RGB_sensor_path=RGB_sensor_path,   
                                                   domain=domain,
                                                   site=site,
                                                   elevation=elevation,
                                                   heights=heights,
                                                   species_label_dict=species_label_dict,
                                                   HSI_size=self.HSI_size,
                                                   RGB_size=self.RGB_size,                                                   
                                                   savedir=savedir,
                                                   train=train,
                                                   number_of_sites=self.sites,
                                                   number_of_domains=self.domains,
                                                   classes=self.classes,
                                                   chunk_size=chunk_size,
                                                   extend_HSI_box=self.config["train"]["HSI"]["extend_box"],
                                                   extend_RGB_box=self.config["train"]["RGB"]["extend_box"],
                                                   label_column=label_column,
                                                   shuffle=True)

        return created_records

    def calc_class_weight(self):
        """Get class frequency of labels"""

        #Check if train_split has been create
        if not hasattr(self, "train_split"):
            raise ValueError(
                "No training split created, please call DeepTreeAttention.read_data()")

        labels = []
        for image, label in self.train_split:
            labels.append(label)

        #Convert from one_hot
        labels = np.vstack(labels)
        labels = np.argmax(labels, 1)

        class_weights = class_weight.compute_class_weight('balanced', np.unique(labels),
                                                          labels)

        return class_weights

    def create(self, weights=None, submodel=None):
        """Load a model
            Args:
                weights: a saved model weights from previous run
                name: a model name from DeepTreeAttention.models
            """
        self.classes = pd.read_csv(self.classes_file).shape[0]        
        if self.config["train"]["gpus"] > 1:
            self.strategy = tf.distribute.MirroredStrategy()
            print("Running in parallel on {} GPUs".format(self.strategy.num_replicas_in_sync))          
            self.config["train"]["batch_size"] = self.config["train"]["batch_size"] * self.strategy.num_replicas_in_sync
            with self.strategy.scope():
                self.HSI_model, self.HSI_spatial, self.HSI_spectral = Hang.create_models(self.HSI_size, self.HSI_size, self.HSI_channels, self.classes, self.config["train"]["learning_rate"])
                self.RGB_model, self.RGB_spatial, self.RGB_spectral = Hang.create_models(self.RGB_size, self.RGB_size, self.RGB_channels, self.classes, self.config["train"]["learning_rate"])
            
                #create a metadata model
                self.metadata_model = metadata.create(classes=self.classes, sites=self.sites, domains=self.domains, learning_rate=self.config["train"]["learning_rate"])
        else:
            self.HSI_model, self.HSI_spatial, self.HSI_spectral = Hang.create_models(self.HSI_size, self.HSI_size, self.HSI_channels, self.classes, self.config["train"]["learning_rate"])
            self.RGB_model, self.RGB_spatial, self.RGB_spectral = Hang.create_models(self.RGB_size, self.RGB_size, self.RGB_channels, self.classes, self.config["train"]["learning_rate"])
            
            #create a metadata model
            self.metadata_model = metadata.create(classes=self.classes, sites=self.sites, domains=self.domains, learning_rate=self.config["train"]["learning_rate"])
        
    def read_data(self, mode, ids=False, validation_split=False):
        """Read tfrecord into datasets from config
            Args:
                validation_split: True -> split tfrecords into train test. This overrides the evaluation config!
            """
        #Decode mode
        self.train_records = glob.glob(
            os.path.join(self.config["train"]["tfrecords"], "*.tfrecord"))

        if len(self.train_records) == 0:
            raise IOError("Cannot find .tfrecords at {}".format(
                self.config["train"]["tfrecords"]))

        if validation_split:
            print("Splitting training set into train-test")
            train_df = pd.Series(self.train_records)
            #Sample with set seed to make it the same between runs
            self.train_split_records = train_df.head(
                int(self.config["train"]["training_fraction"] * train_df.shape[0])).values
            self.test_split_records = train_df[~(
                train_df.isin(self.train_split_records))].values

            #Create training tf.data
            self.train_split = boxes.tf_dataset(
                tfrecords=self.train_split_records,
                batch_size=self.config["train"]["batch_size"],
                shuffle=self.config["train"]["shuffle"],
                mode=mode,
                ids=ids,
                cache=False,
                augmentation=self.config["train"]["augment"],
                cores=self.config["cpu_workers"])

            #Create testing tf.data
            self.val_split = boxes.tf_dataset(
                tfrecords=self.test_split_records,
                batch_size=self.config["train"]["batch_size"],
                shuffle=False,
                mode=mode,
                ids=ids,
                augmentation=False,
                cache=False,
                cores=self.config["cpu_workers"])
            
            self.val_split_with_ids = boxes.tf_dataset(
                tfrecords=self.test_split_records,
                batch_size=self.config["train"]["batch_size"],                    
                shuffle=False,
                mode=mode,
                ids=True,
                augmentation=False,     
                cache=False,
                cores=self.config["cpu_workers"])                  
        else:
            #Create training tf.data
            self.train_split = boxes.tf_dataset(
                tfrecords=self.train_records,
                batch_size=self.config["train"]["batch_size"],
                shuffle=self.config["train"]["shuffle"],
                mode=mode,
                ids=ids,
                cache=False,
                augmentation=self.config["train"]["augment"],                
                cores=self.config["cpu_workers"])

            #honor config if validation not set
            self.val_split = None
            if self.config["evaluation"]["tfrecords"] is not None:
                self.test_records = glob.glob(
                    os.path.join(self.config["evaluation"]["tfrecords"], "*.tfrecord"))

                self.val_split = boxes.tf_dataset(
                    tfrecords=self.test_records,
                    batch_size=self.config["train"]["batch_size"],                    
                    shuffle=False,
                    mode=mode,
                    ids=ids,
                    augmentation=False,    
                    cache=False,
                    cores=self.config["cpu_workers"])  
                
                self.val_split_with_ids = boxes.tf_dataset(
                    tfrecords=self.test_records,
                    batch_size=self.config["train"]["batch_size"],                    
                    shuffle=False,
                    mode=mode,
                    ids=True,
                    cache=False,
                    augmentation=False,
                    cores=self.config["cpu_workers"])                   
                
    def train(self, experiment=None, class_weight=None, submodel=None, sensor="hyperspectral"):
        """Train a model with callbacks"""
        
        if self.val_split is None:
            print("Cannot run callbacks without validation data, skipping...")
            callback_list = None
        elif experiment is None:
            print("Cannot run callbacks without comet experiment, skipping...")
            callback_list = None
        else:            
            if self.classes_file is not None:
                labeldf = pd.read_csv(self.classes_file)
                label_names = list(labeldf.taxonID.values)
            else:
                label_names = None
                
            callback_list = callbacks.create(log_dir=self.log_dir,
                                             experiment=experiment,
                                             validation_data=self.val_split,
                                             train_data=self.train_split,
                                             label_names=label_names,
                                             train_shp=self.train_shp,
                                             submodel=submodel)
                
        if submodel == "metadata":
            self.metadata_model.fit(
                self.train_split,
                epochs=int(self.config["train"]["metadata"]["epochs"]),
                validation_data=self.val_split,            
                callbacks=callback_list,
                class_weight=class_weight)
        else:
            if submodel == "spatial":
                if sensor == "hyperspectral":
                    self.HSI_spatial.fit(self.train_split,
                                           epochs=int(self.config["train"]["HSI"]["epochs"]),
                                           validation_data=self.val_split,
                                           callbacks=callback_list,
                                           class_weight=class_weight)
                
                elif sensor == "RGB":
                    self.RGB_spatial.fit(self.train_split,
                                                     epochs=int(self.config["train"]["RGB"]["epochs"]),
                                                       validation_data=self.val_split,
                                                       callbacks=callback_list,
                                                       class_weight=class_weight)                
    
            elif submodel == "spectral":
                if sensor == "hyperspectral":
                    self.HSI_spectral.fit(self.train_split,
                                           epochs=int(self.config["train"]["HSI"]["epochs"]),
                                           validation_data=self.val_split,
                                           callbacks=callback_list,
                                           class_weight=class_weight)
                elif sensor == "RGB":
                    self.RGB_spectral.fit(
                        self.train_split,
                        epochs=int(self.config["train"]["RGB"]["epochs"]),
                        validation_data=self.val_split,
                        callbacks=callback_list,
                        class_weight=class_weight)      
            else:
                if sensor == "hyperspectral":
                    self.HSI_model.fit(
                        self.train_split,
                        epochs=self.config["train"]["HSI"]["epochs"],
                        validation_data=self.val_split,
                        callbacks=callback_list,
                        class_weight=class_weight)
                
                elif sensor == "RGB":
                    self.RGB_model.fit(
                        self.train_split,
                        epochs=self.config["train"]["RGB"]["epochs"],
                        validation_data=self.val_split,
                        callbacks=callback_list,
                        class_weight=class_weight)
        
    def ensemble(self, experiment, class_weight=None, freeze = True, train=True):
        self.classes = pd.read_csv(self.classes_file).shape[0] 
        
        self.read_data(mode="ensemble")      
        
        if self.val_split is None:
            print("Cannot run callbacks without validation data, skipping...")
            callback_list = None
            label_names = None
        elif experiment is None:
            print("Cannot run callbacks without comet experiment, skipping...")
            callback_list = None
            label_names = None
        else:            
            if self.classes_file is not None:
                labeldf = pd.read_csv(self.classes_file)
                label_names = list(labeldf.taxonID.values)
            else:
                label_names = None
                
            callback_list = callbacks.create(log_dir=self.log_dir,
                                             experiment=experiment,
                                             validation_data=self.val_split,
                                             train_data=self.train_split,
                                             label_names=label_names,
                                             train_shp=self.train_shp,    
                                             submodel="ensemble")
            
            print("callback list is {}".format(callback_list))
        
        if self.config["train"]["gpus"] > 1:
            with self.strategy.scope():        
                self.ensemble_model = Hang.learned_ensemble(HSI_model=self.HSI_model, RGB_model=self.RGB_model, metadata_model= self.metadata_model, freeze=freeze, classes=self.classes)
                
                if train:
                    self.ensemble_model.compile(
                        loss="categorical_crossentropy",
                        optimizer=tf.keras.optimizers.Adam(
                        lr=float(self.config["train"]["learning_rate"])),
                        metrics=[tf.keras.metrics.CategoricalAccuracy(
                                                                     name='acc')])
                    #Train ensemble layer
                    self.ensemble_model.fit(
                        self.train_split,
                        epochs=self.config["train"]["ensemble"]["epochs"],
                        validation_data=self.val_split,
                        callbacks=callback_list,
                        class_weight=class_weight)                    
        else:
            self.ensemble_model = Hang.metadata_ensemble(HSI_model=self.HSI_model,metadata_model=self.metadata_model, classes=self.classes)
            if train:
                self.ensemble_model.compile(
                    loss="categorical_crossentropy",
                    optimizer=tf.keras.optimizers.Adam(
                    lr=float(self.config["train"]["learning_rate"])),
                    metrics=[tf.keras.metrics.CategoricalAccuracy(
                                                                 name='acc')])            
                        
                #Train ensemble layer
                self.ensemble_model.fit(
                    self.train_split,
                    epochs=self.config["train"]["ensemble"]["epochs"],
                    validation_data=self.val_split,
                    callbacks=callback_list,
                    class_weight=class_weight)
        
    def ensemble_predict(self):
        """Predict species id for each box in a single shapefile
        Args:
            shapefile: path to a shapefile
            record_dirname: directory to save generated records
            create_records: overwrite previous records
        Returns:
            fname: path to predicted shapefile
        """
        #Get the true labels since they are not shuffled
        y_true = [ ]
        y_pred = [ ]
        box_index = [ ]
        for index, batch in self.val_split_with_ids:
            data,label = batch
            prediction = self.ensemble_model.predict_on_batch(data)            
            y_true.append(label)
            y_pred.append(prediction)
            box_index.append(index)            
            
        y_true = np.concatenate(y_true)
        y_pred = np.concatenate(y_pred)
        box_index = np.concatenate(box_index)
        box_index = list(box_index)
        
        y_true = np.argmax(y_true, 1)
        y_pred = np.argmax(y_pred, 1)
            
        results = pd.DataFrame({"true":y_true,"predicted":y_pred, "box_index":box_index})
        results["id"] = results["box_index"].apply(lambda x: int(x.decode("utf-8")))
        
        #Read original data        
        shapefile = self.config["evaluation"]["ground_truth_path"]
        gdf = gpd.read_file(shapefile)        

        #Merge
        joined_gdf = gdf.merge(results, on="id")
        
        joined_gdf = joined_gdf.drop(columns=["box_index"])
        
        labeldf = pd.read_csv(self.classes_file)
        label_names = list(labeldf.taxonID.values)
        
        joined_gdf["true_taxonID"] = joined_gdf.true.apply(lambda x: label_names[x])
        joined_gdf["predicted_taxonID"] = joined_gdf.predicted.apply(lambda x: label_names[x])
        
        print(joined_gdf.dtypes)
        
        return joined_gdf