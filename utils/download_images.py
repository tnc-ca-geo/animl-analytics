########
#
# download_images.py
#
# Iterates over a COCO for Camera Traps format export from Animl
# and downloads the images from the archive S3 bucket to a local, user-supplied
# directory. Max threads sets parallelization capacity to hurry the operation.
#
# Usage:
# 
# python3 utils/download_images.py \
#   --coco-file ./outputs/elephants.json \
#   --output-dir ./outputs \
#   --max-threads 50
########

import os
import json
import argparse
from pathlib import Path
from queue import Queue
import boto3
import threading
from tqdm import tqdm
import math

parser = argparse.ArgumentParser()
parser.add_argument("--coco-file", help = "path to coco file")
parser.add_argument("--output-dir", help = "local directory to download images to")
parser.add_argument("--max-threads", help = "Maximum # of threads for parallelization")
args = parser.parse_args()

os.environ['AWS_PROFILE'] = 'animl'
os.environ['AWS_DEFAULT_REGION'] = 'us-west-2'
sess = boto3.Session()
client = boto3.client('s3')

ENV = 'prod'
SERVING_BUCKET = f'animl-images-serving-{ENV}'

def download_image_file(dest_dir, pbar, src_bkt=SERVING_BUCKET):
    while not img_files_q.empty():
        img_record = img_files_q.get()
        key = img_record["serving_bucket_key"]
        relative_dest = img_record["file_name"]

        try: 
            full_dest_path = os.path.join(dest_dir, relative_dest)
            Path(full_dest_path).parents[0].mkdir(parents=True, exist_ok=True)
            client.download_file(src_bkt, key, full_dest_path)

            #Update progress bar
            pbar.update()

        except Exception as e:
            print(f"An exception occurred while downloading {key}:") 
            print(e)
            return

    return

def load_json(file):
    with open(file) as json_file:
        data = json.load(json_file)
        return data

if __name__ == "__main__":
    if args.coco_file and args.output_dir:
        if not os.path.exists(args.output_dir):
            os.makedirs(args.output_dir)
        cct = load_json(args.coco_file)

        if len(cct['images']) == 0:
            print("No image records included in coco file")
            exit()

        #Create and load request queue
        img_files_q = Queue()
        for _image in cct['images']:
            img_files_q.put(_image)

        #Print description
        print(f"Downloading {img_files_q.qsize()} image files to {args.output_dir}")
    
        #Create progress bar
        pbar = tqdm(total=img_files_q.qsize(), delay=1)

        #Find max threads on machine
        if args.max_threads:
            max_threads = int(args.max_threads)
        else: max_threads = 50

        #Dynamically allocate threads
        thread_count = min(max_threads, int(img_files_q.qsize() ** (1/3)))
        print(f"Running operation on {thread_count} threads")

        #Create and start worker threads
        threads = []
        for i in range(thread_count):
            thread = threading.Thread(target = download_image_file, kwargs={'dest_dir' : args.output_dir, 'pbar': pbar})
            thread.start()
            threads.append(thread)

        #Wait for threads to complete
        for thread in threads:
            thread.join()

        print("\n")
        print("All files have been downloaded.")

    else:
        print("Supply a COCO file and output directory")
        print("Run download_images.py --help for usage info")
