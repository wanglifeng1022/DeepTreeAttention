#Context module. Use a pretrain model to extract the penultimate layer of the model for surrounding trees.
import tensorflow as tf
import rasterio

from DeepTreeAttention.generators.boxes import crop_image, resize
from DeepTreeAttention.utils.paths import find_sensor_path, elevation_from_tile

from sklearn.neighbors import BallTree
import numpy as np
import pandas as pd

def get_nearest(src_points, candidates, k_neighbors=1):
    """Find nearest neighbors for all source points from a set of candidate points"""

    # Create tree from the candidate points
    coordinates = np.vstack(candidates.geometry.centroid.apply(lambda geom: (geom.x,geom.y)))
    tree = BallTree(coordinates, leaf_size=15, metric='haversine')

    # Find closest points and distances
    #src_points = src_points.reset_index()    
    src_x = src_points.geometry.centroid.x
    src_y = src_points.geometry.centroid.y
    
    src_points = np.array([src_x, src_y]).reshape(-1,2)
    distances, indices = tree.query(src_points, k=k_neighbors)
    
    neighbor_geoms = candidates[candidates.index.isin(indices[0])]
    neighbor_geoms["distance"] = distances[0]

    # Return indices and distances
    return neighbor_geoms

def predict_neighbors(target, HSI_size, neighbor_pool, metadata, raster, model, k_neighbors=5):
    """Get features of surrounding n trees
    Args:
    target: geometry object of the target point
    neighbor_pool: geopandas dataframe with points
    metadata: The metadata layer for each of the points, assumed to be identical for all neighbors
    n: Number of neighbors
    model: A model object to predict features
    Returns:
    n * m feature matrix, where n is number of neighbors and m is length of the penultimate model layer
    """
        
    #Find neighbors
    neighbor_geoms = get_nearest(target, candidates = neighbor_pool , k_neighbors=k_neighbors)
    
    #extract crop for each neighbor
    features = [ ]
    distances = [ ]
    for index, row in neighbor_geoms.iterrows():
        crop = crop_image(src=raster, box=row["geometry"])
        
        #reorder to channels last
        crop = resize(crop, HSI_size, HSI_size)
        crop = np.expand_dims(crop, 0)
        
        #create batch
        elevation = np.expand_dims(metadata[0],axis=0)
        site = np.expand_dims(metadata[1],axis=0)
        domain = np.expand_dims(metadata[2],axis=0)
        
        batch  = [crop,elevation,site,domain]
        feature = model(batch)
        features.append(feature)
        distances.append(row["distance"])
    
    features = np.vstack(features)
    
    return features, distances

def extract_features(df, x, model_class, hyperspectral_pool, site_label_dict, domain_label_dict, HSI_size=20, k_neighbors=5):
    """Generate features
    Args:
    df: a geopandas dataframe
    x: individual id to use a target
    model_class: A deeptreeattention model class to extract layer features
    hyperspectral_pool: glob dir to search for sensor files
    HSI_size: size of HSI crop
    site_label_dict: dictionary of numeric site labels
    domain_label_dict: dictionary of numeric domain labels
    k_neighbors: number of neighbors to extract
    Returns:
    feature_array: a feature matrix of encoded bottleneck layer
    """
    #Due to resampling, there will be multiple rows of the same point, all are identical.
    target  =  df[df.individual == x].head(1)
    target = target.reset_index(drop=True)
    sensor_path = find_sensor_path(bounds=target.total_bounds, lookup_pool=hyperspectral_pool) 
    
    #Encode metadata
    site = target.siteID.values[0]
    numeric_site = site_label_dict[site]
    one_hot_sites = tf.one_hot(numeric_site, model_class.sites)
    
    domain = target.domainID.values[0]
    numeric_domain = domain_label_dict[domain]   
    one_hot_domains = tf.one_hot(numeric_domain, model_class.domains)
    
    #ToDO bring h5 into here.
    #elevation = elevation_from_tile(sensor_path)/1000
    elevation = 100/1000
    metadata = [elevation, one_hot_sites, one_hot_domains]
    
    neighbor_pool = df[~(df.individual == x)].reset_index(drop=True)
    raster = rasterio.open(sensor_path)
    feature_array, distances = predict_neighbors(target, metadata=metadata, HSI_size=HSI_size, raster=raster, neighbor_pool=neighbor_pool, model=model_class.ensemble_model, k_neighbors=k_neighbors)
    
    return feature_array, distances

    
def predict_dataframe(df, model_class, hyperspectral_pool, site_label_dict, domain_label_dict, HSI_size=20, k_neighbors=5):
    """Iterate through a geopandas dataframe and get neighbors for each tree.
    Args:
    df: a geopandas dataframe
    model_class: A deeptreeattention model class to extract layer features
    hyperspectral_pool: glob dir to search for sensor files
    HSI_size: size of HSI crop
    site_label_dict: dictionary of numeric site labels
    domain_label_dict: dictionary of numeric domain labels
    k_neighbors: number of neighbors to extract
    Returns:
    feature_array: a feature matrix of encoded bottleneck layer
    """
    
    #for each target in a dataframe, lookup the correct tile
    neighbor_features = {}
    for index, row in df.iterrows():  
        row = pd.DataFrame(row).transpose()
        neighbor_features[index] = extract_features(
            df=df,
            x=row["individual"].values[0],
            model_class=model_class,
            hyperspectral_pool=hyperspectral_pool,
            site_label_dict=site_label_dict,
            domain_label_dict=domain_label_dict
        )
        
    return neighbor_features