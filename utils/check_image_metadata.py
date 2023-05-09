#
# check_image_metadata.py
#
# evaluates a local directory of images for presence of metadata required for
# ingestion in Animl
#

#%% Imports

import os
import json
from pathlib import Path
from tqdm import tqdm
from collections import defaultdict 
import exiftool

#%% Main function


def get_exif(files):
    try:
        with exiftool.ExifToolHelper() as et:
            metadata = et.get_metadata(files)
            return metadata
    except exiftool.exceptions.ExifToolExecuteError:
        return print(f'error getting exif data')

def evaluate_images(input_dir,output_file=None):

    if output_file is None:
        output_file = './outputs/output.csv'

    if not os.path.isdir(input_dir):
        print(f'{input_dir} is not a directory')
        return
    
    print(f'Building list of image paths in {input_dir}...')

    #%% Get exif data

    image_paths = []
    for root, dir_names, file_names in os.walk(input_dir):
        for f in file_names:
            if f.lower().endswith(('.jpg', '.jpeg')):
                image_paths.append(os.path.join(root, f))
   
    print(f'Getting EXIF data for {len(image_paths)} images...')

    metadata = get_exif(image_paths)
    print(f'medadata[0]: {metadata[0]}')

    ##%% Write output file
    
    print('Writing output file...')
    
    with open(output_file,'w') as f:
        
        f.write('relative_path,date_time_original,file_size,file_too_big,make,model,serial_number,user_comment\n')
        
        # im = images[0]
        for im in tqdm(metadata):
            
            relative_path = im['SourceFile']

            if 'EXIF:DateTimeOriginal' in im:
                date_time_original = im['EXIF:DateTimeOriginal']
            else: 
                date_time_original = 'unknown'

            if 'File:FileSize' in im:
                file_size = im['File:FileSize']
            else: 
                file_size = 'unknown'

            if 'File:FileSize' in im:
                file_too_big = im['File:FileSize'] > 4000000
            else: 
                file_too_big = 'unknown'

            if 'EXIF:Make' in im:
                make = im['EXIF:Make']
            else: 
                make = 'unknown'
                
            if 'EXIF:Model' in im:
                model = im['EXIF:Model']
            else: 
                model = 'unknown'

            if 'EXIF:SerialNumber' in im:
                serial_number = im['EXIF:SerialNumber']
            else: 
                serial_number = 'unknown'

            if 'EXIF:UserComment' in im:
                user_comment = im['EXIF:UserComment']
            else:
                user_comment = 'unknown'

            f.write('{},{},{},{},{},{},{},{}\n'.format(relative_path,
                   date_time_original,file_size,file_too_big,make,model,serial_number,user_comment))

#%% Command-line driver

import argparse

def main():

    parser = argparse.ArgumentParser(description=('evaluates a local directory of images for presence of metadata required for ingestion in Animl'))

    parser.add_argument('input_dir', type=str)                        
    parser.add_argument('--output_file', type=str, default=None)
    
    args = parser.parse_args()
    evaluate_images(args.input_dir, args.output_file)
    
if __name__ == '__main__':
    main()
