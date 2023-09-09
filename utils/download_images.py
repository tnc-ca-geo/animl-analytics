########
#
# download_images.py
#
# Iterates over a COCO for Camera Traps format export from Animl
# and downloads the images from the archive S3 bucket to a local, user-supplied
# directory
#
# Usage:
# 
# python utils/download_images.py 
#   --coco-file ~/Downloads/5398d25a25a8b018ce08c9ceb475de36_coco.json 
#   --output-dir ./outputs
########

import os
import json
import argparse
from pathlib import Path
import boto3
from tqdm import tqdm

parser = argparse.ArgumentParser()
parser.add_argument("--coco-file", help = "path to coco file")
parser.add_argument("--output-dir", help = "local directory to download images to")
args = parser.parse_args()

os.environ['AWS_PROFILE'] = 'animl'
os.environ['AWS_DEFAULT_REGION'] = 'us-west-2'
sess = boto3.Session()

SERVING_BUCKET = 'animl-images-serving-dev'

def download_image_files(img_rcrds, dest_dir, src_bkt=SERVING_BUCKET):
    print(f"Downloading {len(img_rcrds)} image files to {dest_dir}")
    i = 0
    for rec in tqdm(img_rcrds):
        key = rec["serving_bucket_key"]
        relative_dest = rec["file_name"]
        try: 
            full_dest_path = os.path.join(dest_dir, relative_dest)
            Path(full_dest_path).parents[0].mkdir(parents=True, exist_ok=True)
            boto3.client('s3').download_file(src_bkt, key, full_dest_path)
            i += 1
        except Exception as e:
            print(f"An exception occurred while downloading {key}:") 
            print(e)
    print(f'Successfully downloaded {i} images')

def load_json(file):
    with open(file) as json_file:
        data = json.load(json_file)
        return data

if __name__ == "__main__":
    if args.coco_file and args.output_dir:
        if not os.path.exists(args.output_dir):
            os.makedirs(args.output_dir)
        cct = load_json(args.coco_file)
        download_image_files(cct["images"], args.output_dir)
    else:
        print("Supply a COCO file and output directory")
        print("Run download_images.py --help for usage info")

