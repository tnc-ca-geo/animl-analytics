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
import boto3

parser = argparse.ArgumentParser()
parser.add_argument("--coco-file", help = "path to coco file")
parser.add_argument("--output-dir", help = "local directory to download images to")
args = parser.parse_args()

os.environ['AWS_PROFILE'] = 'animl'
os.environ['AWS_DEFAULT_REGION'] = 'us-west-2'
sess = boto3.Session()

ARCHIVE_BUCKET = 'animl-images-archive-prod'

def download_image_files(img_rcrds, dest_dir, src_bkt=ARCHIVE_BUCKET):
    print(f"Downloading {len(img_rcrds)} image files to {dest_dir}")
    # TODO: display progress bar
    i = 0
    for rec in img_rcrds:
        key = rec["original_relative_path"]
        camera = key.split("/")[0]
        camera_dir = os.path.join(dest_dir, camera)
        if not os.path.exists(camera_dir):
            os.makedirs(camera_dir)
        try: 
            filename = os.path.join(dest_dir, key)
            print(filename)
            boto3.client('s3').download_file(src_bkt, key, filename)
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

