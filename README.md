# Animl Analytics
Utilities and documentation for analyzing Animl camera trap data

## `Related repos`
- Animl Frontend          http://github.com/tnc-ca-geo/animl-frontend
- Animl API               http://github.com/tnc-ca-geo/animl-api
- Animl ML                http://github.com/tnc-ca-geo/animl-ml

## `Intro`

This repo contains utility functions, notebooks, and scripts for analyzing image data stored in Animl.

### `Data structure & schema`
- TODO: explain where image data is stored (records in MongoDB, image files in S3)
- TODO: document image record schema

### `Set up & permissions`
NOTE: be sure that you have the following installed:
 - [awscli](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html). Make sure to configure your awscli config profile name is `"animl"`.

- TODO: permissions setup needed for accessing MongoDB, S3, invoking Sagemaker (creating .env file)

#### Clone the repo and set up a virtual env at the project root level

```
$ mkdir animl-analytics
$ git clone https://github.com/tnc-ca-geo/animl-analytics.git
$ cd animl-analytics
$ python3 -m venv venv
$ source venv/bin/activate
$ pip3 install -r requirements.txt
```

Secrets are managed in a `.env` file that you'll need to create manually at the root directory level. Create the file:
```
$ touch .env
```
and add the following to it, replacing the credentials in angle brackets with your own creds:
```
MONGODB_URL=mongodb+srv://<user>:<password>@cluster0-bqyly.mongodb.net/<database>?retryWrites=true&w=majority
```

*Note: if you install additional packages/dependencies, add them to requirements.txt with `pip freeze > requirements.txt`*

## `Querying the database through the Animl frontend`
- TODO: explain how to use Custom Filters, point to MongoDB query docs, give some examples/templates for useful queries

## `Exporting data`
Image record data from MongoDB can be exported to CSV or [COCO for Camera Traps format](https://github.com/Microsoft/CameraTraps/blob/main/data_management/README.md#coco-cameratraps-format). The CSV export schema is compatible with [CameratrapR](https://github.com/jniedballa/camtrapR)–and thus useful for ecological analyses–while COCO for Camera Traps format includes higher-fidelity information about the images' annotations (labels) and their bounding-boxes and is therefor better suited for ML model training. COCO for Camera Traps is compatible with many of the utilities found in Microsoft's [CameraTraps](https://github.com/microsoft/CameraTraps) repo.

To export data from Animl, you'll need to have a user account with Project Manager permissions. Once logged in and once you have applied your desired filters, at the bottom of the filters panel there is an Export Data button, which will open a dialogue box and step you through the remainder of the export process.

## `Document utility functions`
### `Downloading images`
To download images to your local computer, first export the image record data in COCO for Camera Traps format from the Animl user interface (see instructions above). Next, if it's not already running, start up the virtual env by running `source venv/bin/activate` from the rood directory of this project. Also from the root directory of this project, you can then run:

```bash
python utils/download_images.py \
   --coco-file <path/to/coco_export.json> \
   --output-dir ./outputs/<subdirectory>
```

which will export all of the images in your COCO file to your output directory.

- TODO: functions for querying MongoDB

## `Directory of resources`
- TODO: document notebooks and their uses
