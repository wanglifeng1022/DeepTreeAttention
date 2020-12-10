#Create spectral - spatial fusion model
from .layers import *
from tensorflow.keras import metrics

def define_model(height=11, width=11, channels=48, classes=2, weighted_sum=False, softmax=True):
    """
    Create model and return output layers to allow training at different levels
    """
    input_shape = (height, width, channels)
    sensor_inputs = layers.Input(shape=input_shape, name="data_input")
    
    #spatial subnetwork and weak attention classifications
    spatial_attention_outputs, spatial_attention_pool = spatial_network(sensor_inputs, classes=classes)

    #spectral network
    spectral_attention_outputs, spectral_attention_pool = spectral_network(sensor_inputs, classes=classes)

    #Learn weighted average of just the final conv
    if softmax:
        sensor_output = submodule_consensus(
            spatial_attention_outputs[2],
            spectral_attention_outputs[2],
            weighted_sum=weighted_sum)
    else:
        sensor_output = submodule_consensus(
            spatial_attention_pool[2],
            spectral_attention_pool[2],
            weighted_sum=weighted_sum)
        
    return sensor_inputs, sensor_output, spatial_attention_outputs, spectral_attention_outputs

def create_models(height, width, channels, classes, learning_rate, weighted_sum=True):
    #Define model structure
    sensor_inputs, sensor_outputs, spatial_attention_outputs, spectral_attention_outputs = define_model(
        height = height,
        width = width,
        channels = channels,
        classes = classes,
        weighted_sum=weighted_sum, softmax=True)

    #Full model compile
    model = tf.keras.Model(inputs=sensor_inputs,
                                outputs=sensor_outputs,
                                name="DeepTreeAttention")

    #compile full model
    metric_list = [metrics.CategoricalAccuracy(name="acc")]    
    model.compile(loss="categorical_crossentropy",
                       optimizer=tf.keras.optimizers.Adam(
                           lr=float(learning_rate)),
                       metrics=metric_list)
    #compile
    loss_dict = {
        "spatial_attention_1": "categorical_crossentropy",
        "spatial_attention_2": "categorical_crossentropy",
        "spatial_attention_3": "categorical_crossentropy"
    }

    # Spatial Attention softmax model
    spatial_model = tf.keras.Model(inputs=sensor_inputs,
                                        outputs=spatial_attention_outputs,
                                        name="DeepTreeAttention")

    spatial_model.compile(
        loss=loss_dict,
        loss_weights=[0.01, 0.1, 1],
        optimizer=tf.keras.optimizers.Adam(
            lr=float(learning_rate)),
        metrics=metric_list)

    # Spectral Attention softmax model
    spectral_model = tf.keras.Model(inputs=sensor_inputs,
                                         outputs=spectral_attention_outputs,
                                         name="DeepTreeAttention")

    #compile loss dict
    loss_dict = {
        "spectral_attention_1": "categorical_crossentropy",
        "spectral_attention_2": "categorical_crossentropy",
        "spectral_attention_3": "categorical_crossentropy"
    }

    spectral_model.compile(
        loss=loss_dict,
        loss_weights=[0.01, 0.1, 1],
        optimizer=tf.keras.optimizers.Adam(
            lr=float(learning_rate)),
        metrics=metric_list)
    
    return model, spatial_model, spectral_model

def strip_sensor_softmax(model, classes, index, squeeze=False, squeeze_size=128):
    #prepare RGB model
    spectral_relu_layer = model.get_layer("spectral_pooling_filters_128").output
    spatial_relu_layer = model.get_layer("spatial_pooling_filters_128").output
    weighted_relu = WeightedSum(name="within_model_weighted_" + index)([spectral_relu_layer, spatial_relu_layer])
    
    if squeeze:
        weighted_relu = layers.Dense(squeeze_size)(weighted_relu)
        
    stripped_model = tf.keras.Model(inputs=model.inputs, outputs = weighted_relu)
    for x in model.layers:
        x._name = x.name + str(index)
    
    return stripped_model

def metadata_ensemble(HSI_model, metadata_model, classes):
    normalized_metadata = layers.BatchNormalization()(metadata_model.get_layer("last_relu").output)
    
    #for each of the spatial and spectral layers, fuse metadata
    fused_layers = {}
    for x in ["spectral","spatial"]:
        for y in [128]:
            class_pooling = HSI_model.get_layer("{}_pooling_filters_{}".format(x,y)).output
            fused_layers["{}_{}".format(x,y)] = metadata_fusion(class_pooling, normalized_metadata, classes, label="{}_{}".format(x,y))
    
    ensemble_softmax = WeightedSum(name="weighted_sum")([fused_layers["spatial_128"], fused_layers["spectral_128"]])
    
    ensemble_model = tf.keras.Model(inputs=HSI_model.inputs+metadata_model.inputs,
                                    outputs=ensemble_softmax,
                           name="ensemble_model")    
    
    return ensemble_model

def learned_ensemble(RGB_model, HSI_model, metadata_model, classes):
    stripped_HSI_model = strip_sensor_softmax(HSI_model, classes, index = "HSI", squeeze=True, squeeze_size=classes)      
    normalized_metadata = layers.BatchNormalization()(metadata_model.get_layer("last_relu").output)
    stripped_metadata = tf.keras.Model(inputs=metadata_model.inputs, outputs = normalized_metadata)
    
    #concat and learn ensemble weights
    merged_layers = layers.Concatenate(name="submodel_concat")([stripped_HSI_model.output, stripped_RGB_model.output, stripped_metadata.output])    
    merged_layers = layers.Dropout(0.7)(merged_layers)
    ensemble_softmax = layers.Dense(classes,name="ensemble_learn",activation="softmax")(merged_layers)

    #Take joint inputs    
    ensemble_model = tf.keras.Model(inputs=HSI_model.inputs+RGB_model.inputs+metadata_model.inputs,
                                    outputs=ensemble_softmax,
                           name="ensemble_model")    
    
    return ensemble_model


def fuse(rgb, hsi, metadata, classes):
    """Fuse a rgb attention and an hsi attention layer"""
    fused_spatial = tf.keras.layers.Multiply()([rgb,hsi])  
    fused_spatial = tf.keras.layers.Conv2D(1, (1, 1), activation="relu")(fused_spatial)
    fused_spatial = tf.keras.layers.Add()([hsi, fused_spatial])
    
    class_pool = layers.MaxPool2D((1, 1))(fused_spatial)
    class_pool = layers.Flatten()(class_pool)
    class_pool = metadata_fusion(class_pool, metadata, classes)    
    output = layers.Dense(classes,
                          activation="softmax")(class_pool)    
    
    return output
    
def spatial_ensemble(RGB_model, HSI_model, metadata_model, classes):
    
    #Normalize metadata
    normalized_metadata = layers.BatchNormalization()(metadata_model.get_layer("last_relu").output)
    
    #unique names
    for x in RGB_model.layers:
        x._name = x.name + str("RGB")
    
    for x in HSI_model.layers:
        x._name = x.name + str("HSI")  
        
    #Get spatial attention layer from RGB(remove 'RGB' name TODO)
    rgb_spatial_attention = RGB_model.get_layer("SpatialAttentionConv_3RGB").output
    
    #resize to output
    downsampled_rgb = tf.keras.layers.AveragePooling2D(pool_size=(5,5))(rgb_spatial_attention)
    downsampled_rgb = tf.keras.layers.BatchNormalization()(downsampled_rgb)
    
    #Get the Final spatial and spectral attention CONV layers
    HSI_spatial_attention = HSI_model.get_layer("SpatialAttentionConv_3HSI").output
    HSI_spectral_attention = HSI_model.get_layer("SpectralAttentionConv_3HSI").output
    
    #Fuse spatial
    spatial_fused_output = fuse(rgb=downsampled_rgb, hsi=HSI_spatial_attention, metadata=normalized_metadata, classes=classes)

    #Fuse spectral
    spectral_fused_output = fuse(rgb=downsampled_rgb, hsi=HSI_spectral_attention, metadata=normalized_metadata, classes=classes)

    ensemble_softmax = WeightedSum()([spatial_fused_output, spectral_fused_output])
    
    #Take joint inputs    
    ensemble_model = tf.keras.Model(inputs=HSI_model.inputs+RGB_model.inputs+metadata_model.inputs,
                                    outputs=ensemble_softmax,name="ensemble_model")    
    
    return ensemble_model