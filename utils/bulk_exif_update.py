#
# bulk_exif_update.py
#
# takes csv output of check_image_metadata.py and writes all serial numbers
# to the images' exif. Also copies updated images to new directory.
#

#%% Imports

import os
import csv
import shutil 
from pathlib import Path
from tqdm import tqdm
from collections import defaultdict 
import exiftool

ROOT_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), '..'))

#%% Main function

def write_exif(fp, tags):
    try:
        with exiftool.ExifToolHelper() as et:
            et.set_tags(fp,tags)
    except exiftool.exceptions.ExifToolExecuteError:
        return print(f'error setting exif data')

def csv_to_exif(input_csv,output_dir=None):
    
    input_csv = os.path.abspath(input_csv)
    print(f'input csv: {input_csv}')
    
    if not os.path.isfile(input_csv) or not input_csv.lower().endswith('.csv'):
        print(f'{input_csv} is not a valid CSV')
        return

    if output_dir is None:
        output_dir = './outputs/' + Path(input_csv).stem + '_modified/'
    
    print(f'Reading in {input_csv}')
    print(f'output_dir {output_dir}')

    images = []
    with open(input_csv) as fp:
        reader = csv.reader(fp, delimiter=",", quotechar='"')
        images = [{k: v for k, v in row.items()} 
             for row in csv.DictReader(fp, skipinitialspace=True)]
    print(f'images[0]: {images[0]}')

    for im in tqdm(images):
        if not 'serial_number' in im or im['serial_number'] in ('unknown', ''):
            print(f'skipping b/c no serial number in {im}')
            continue
        # set serial number
        write_exif(im['absolute_path'], {"SerialNumber": im['serial_number']})

        # copy to output dir
        file_name = os.path.basename(im['absolute_path'])
        dest = os.path.join(output_dir, file_name)
        dest_parent_dir = Path(dest).parent.absolute()
        if not os.path.exists(dest_parent_dir):
            os.mkdir(dest_parent_dir)
        shutil.move(im['absolute_path'], dest)

        # remove exiftol-generated "_original" from original filenames
        os.rename(im['absolute_path'] + '_original', im['absolute_path'])

#%% Command-line driver

import argparse

def main():

    parser = argparse.ArgumentParser(description=('takes csv output of check_image_metadata.py and writes all serial numbers to the images exif'))

    parser.add_argument('input_csv', type=str)                        
    parser.add_argument('--output_dir', type=str, default=None)
    
    args = parser.parse_args()
    csv_to_exif(args.input_csv, args.output_dir)
    
if __name__ == '__main__':
    main()
