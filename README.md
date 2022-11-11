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
$ source env/bin/activate
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
- TODO: explain how to use Custom Filters, point to MongoDB query docs, give some examples/templates

## `Document utility functions`
- TODO: functions for downloading images from S3
- TODO: functions for querying MongoDB

## `Directory of resources`
- TODO: document notebooks and their uses