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
 - [awscli](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
    - make sure to configure your awscli config profile name is "animl"
 - [virtualenv](https://virtualenv.pypa.io/en/latest/)

- TODO: permissions setup needed for accessing MongoDB, S3, invoking Sagemaker (creating .env file)

#### Clone the repo and set up a virtual env

```
$ mkdir animl-analytics
$ git clone https://github.com/tnc-ca-geo/animl-analytics.git
$ virtualenv env -p python3
$ source env/bin/activate
$ cd animl-analytics
$ pip3 install -r requirements.txt
```

## `Querying the database through the Animl frontend`
- TODO: explain how to use Custom Filters, point to MongoDB query docs, give some examples/templates

## `Document utility functions`
- TODO: functions for downloading images from S3
- TODO: functions for querying MongoDB

## `Directory of resources`
- TODO: document notebooks and their uses