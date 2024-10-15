import numpy as np
import tifffile as tf
from csbdeep.utils import normalize
from stardist.models import StarDist2D
import scanpy as sc
from skimage.draw import disk
import pandas as pd
import imagecodecs
import fire
import warnings
import squidpy as sq
from skimage.measure import grid_points_in_poly
warnings.filterwarnings("ignore")
import json
import os
import gzip
import numpy as np
def segmentation(img, prob_thresh=0.3, nms_thresh=0.4, pmin=3, pmax = 99.8):
    model = StarDist2D.from_pretrained('2D_versatile_he')

    normalised = normalize(img, pmin, pmax)
    label_fluo, poly_fluo = model.predict_instances(
        normalised,
        n_tiles=(10, 10, 1),
        prob_thresh=prob_thresh,
        nms_thresh=nms_thresh,
    )
    return label_fluo, poly_fluo, normalised
    
    
def define_area_aspect_ratio_ellipse(x,y):
    #fit perimeter to ellipse and find its major axes
    AA = np.stack([x**2, x * y, y**2, x, y]).T
    bb = np.ones_like(x)
    A, B, C, D, E = np.linalg.lstsq(AA, bb)[0].squeeze() # here I fit points to the ellipse
    #that's formulas for genearal ellipse, using fitted par-s https://en.wikipedia.org/wiki/Ellipse
    F = -1 #we forced the constant to be -1 when did fittting (see bb and actual formula)
    a = -np.sqrt(2*(A*E**2 + C*D**2 - B*D*E + (B**2-4*A*C)*F) * (A + C + np.sqrt((A-C)**2+B**2)))/(B**2-4*A*C)
    b = -np.sqrt(2*(A*E**2 + C*D**2 - B*D*E + (B**2-4*A*C)*F) * (A + C - np.sqrt((A-C)**2+B**2)))/(B**2-4*A*C)
    elongation = np.max([a,b])/np.min([a,b])
    #compute area by the number of pixels which are isnide the perimeter x,y
    tuple_list_xy = [(xx-np.min(x), yy-np.min(y)) for xx, yy in zip(x,y)]
    inside = grid_points_in_poly((int(np.max(x))-int(np.min(x))+1, int(np.max(y))-int(np.min(y))+1), tuple_list_xy)
    area = np.sum(inside)
    return area, elongation

def one_visium_spot_analysis(yx, spot_radius, label_fluo, poly_fluo, mask_img, img, scale_factor):
    yx = np.array([int(yx[0]), int(yx[1])])*scale_factor
    rr, cc = disk(yx, spot_radius*scale_factor)
    vals = label_fluo[cc, rr]
    ids0, counts = np.unique(vals, return_counts=True) #ids are actually cell ids which are sitting on top of visium spot
    ids = ids0[ids0 != 0]
    n_cell = len(ids)
    occupancy = np.sum(counts[ids0 != 0])/len(rr)
    non_bg_mask = vals != 0
    R,G,B = np.sum(img[cc, rr][non_bg_mask], axis=0)
    if n_cell>0:
        #here I look for additional cells par-s
        segm_prob = np.mean(poly_fluo['prob'][ids-1])
        area_list = []; elongation_list = []
        for id_i in ids:
            x_contour = poly_fluo['coord'][id_i-1,0,:]; y_contour = poly_fluo['coord'][id_i-1,1,:]
            ar, el = define_area_aspect_ratio_ellipse(x_contour, y_contour)
            area_list.append(ar); 
            if np.isnan(el)==False:
                elongation_list.append(el)
                
        if len(area_list)>0:
            area = np.mean(np.array(area_list))
        else:
            area = 0
        if len(elongation_list)>0:
            elongation = np.mean(np.array(elongation_list))
        else:
            elongation = 0
    else:
        area = 0; elongation = 0; segm_prob = 0
    
    #here I look for tissue_coverage
    rr2 = rr.astype(int); cc2 = cc.astype(int)
    empty_space_px = np.sum(mask_img[cc2,rr2])
    occupancy_tissue = 1 - empty_space_px/rr.shape[0]
    
    return n_cell, occupancy, R, G, B, area, elongation, segm_prob, occupancy_tissue
    

def save_segmentation_features(adata, sample_name_id, img, background_thresh, label_fluo, poly_fluo, scale_factor):
    spot_radius = adata.uns['spatial'][sample_name_id]['scalefactors']['spot_diameter_fullres']//2
    yxs = adata.obsm['spatial']
    assert np.max(yxs[:,0]*scale_factor)<img.shape[1] and np.max(yxs[:,1]*scale_factor)<img.shape[0], 'Visium spot positions are out of the image - please check paths for spaceranger outputs and corresponding image'  
    mask_img = (img[:,:,0] > background_thresh) & (img[:,:,2] > background_thresh)
    metrics = []
    for j, yx in enumerate(yxs):  
        n_cell, occupancy, R, G, B, area, elongation, segm_prob, occupancy_tissue = one_visium_spot_analysis(yx,  spot_radius, label_fluo, poly_fluo, mask_img, img, scale_factor)
        metrics.append([yx[0], yx[1], n_cell, occupancy, R, G, B, area, segm_prob, elongation, occupancy_tissue])
    metrics_df = pd.DataFrame(metrics, columns=['y', 'x', 'n_cell', 'occupancy', 'R', 'G', 'B', 'area_cell_px', 'seg_prob', 'elongation', 'occupancy_tissue'], index=adata.obs.index)
    return metrics_df

def save_segmentation_polygons_to_json(poly_dict, sample_name_id, out_folder):
    poly_fluo2 = poly_dict.copy()
    poly_fluo2['coord'] = poly_fluo2['coord'].tolist()
    poly_fluo2['points'] = poly_fluo2['points'].tolist()
    poly_fluo2['prob'] = poly_fluo2['prob'].tolist()
    filepath = os.path.join(out_folder, sample_name_id +'.json')
    with open(filepath, 'w') as f:
        json.dump(poly_fluo2, f)

def save_norm_image_as_compr_npy(img, sample_name, out_folder):
    filepath = os.path.join(out_folder, sample_name +'_norm_image.npy.gz')
    f = gzip.GzipFile(filepath, "w")
    np.save(file=f, arr=img)
    f.close()
    
def save_norm_image_as_compr_tif(normalised, sample_name, out_folder):
    norm_img2 = normalised*255
    norm_img2[norm_img2<0] = 0
    norm_img2[norm_img2>255] = 255
    norm_img2 = norm_img2.astype('uint8')
    print(norm_img2.shape)
    filepath = os.path.join(out_folder, sample_name +'_norm_image.tif')
    tf.imwrite(filepath, norm_img2, imagej=True, compression ='jpeg')
    
def main(img_path, spaceranger_path, sample_name, out_folder, prob_thresh=0.3, nms_thresh=0.4, pmin=3, pmax = 99.8, scale_factor = 1, background_thresh = 200, save_csv = False, save_h5ad = False, save_segm_polygons = True, save_normalised_img = True):
    
    #if there is a new line symbol - remove it
    if '\n' in spaceranger_path:
        spaceranger_path = spaceranger_path.replace('\n', '')
        
    if '\n' in out_folder:
        out_folder = out_folder.replace('\n', '')
        
    adata = sq.read.visium(spaceranger_path)
    img = tf.imread(img_path)
    sample_name_id = list(adata.uns['spatial'].keys())[0]
    
    
    
      
    
    
    label_fluo, poly_fluo, normalised_img = segmentation(img, prob_thresh, nms_thresh, pmin, pmax)
    if save_segm_polygons:
        save_segmentation_polygons_to_json(poly_fluo, sample_name, out_folder)
    
    if save_normalised_img:
        #save_norm_image_as_compr_npy(normalised_img, sample_name, out_folder)
        save_norm_image_as_compr_tif(normalised_img, sample_name, out_folder)
    
    if save_csv or save_h5ad:
        metrics_df = save_segmentation_features(adata, sample_name, img, background_thresh, label_fluo, poly_fluo, scale_factor)
        #save results
        if save_csv:
            full_path_csv = out_folder + '/' + sample_name + '.csv'
            metrics_df.to_csv(full_path_csv)

        if save_h5ad:
            full_path_h5ad = out_folder + '/' + sample_name + '.h5ad'
            adata.obs = pd.concat([adata.obs, metrics_df], axis=1, ignore_index=False)
            adata.write_h5ad(full_path_h5ad)
        
         
if __name__ == "__main__":
    fire.Fire(main)
       

            
            


