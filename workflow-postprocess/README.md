# Workflow Postprocessing

Provides a sample implementation of a Cloud Function designed to execute post-ingestion processing. 
Triggered after data is returned from a external API, the function initiates an event-driven using [Pub/Sub trigger](https://cloud.google.com/run/docs/tutorials/pubsub-eventdriven). It processes incoming raw data, enabling customized transformations or exporting results to downstream systems to allow alignment with your specific business requirements.

## Customizing the Workflow

Copy the file/folder structure of [workflow1](./workflow1/) and rename according to your workflow name. Edit the source code to apply transformations and/or export incoming data.

To create a new workflow, duplicate the file/folder structure of [workflow1](./workflow1/) and rename it to match your workflow's identifier and update the [worflows.json](/config/default/workflows.json) file config.
Edit the source code [src/handler.py](./workflow1/src/handler.py) to implement the aditional data transformations or export logic.

## Test locally

To test and execute your functions locally without deploying, follow these steps:

```sh
cd <WORKFLOW_NAME>
gcloud auth login <your@email.com>
gcloud auth application-default login <your@email.com>

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

PROJECT_ID=<PROJECT_ID> functions-framework --target main --debug
```
For detailed instructions on local deployment, refer to [Cloud Run functions local development](https://cloud.google.com/functions/docs/running/overview?hl=en)