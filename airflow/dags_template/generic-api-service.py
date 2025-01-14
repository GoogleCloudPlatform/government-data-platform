# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import uuid
import datetime
import json
import logging
import pendulum
from airflow.sensors.time_delta import TimeDeltaSensor, TimeDeltaSensorAsync

from airflow import DAG

# from airflow.providers.google.cloud.operators.dataform import DataformRunOperator
from airflow.providers.google.cloud.operators.bigquery import  BigQueryInsertJobOperator, BigQueryCreateEmptyTableOperator, BigQueryDeleteTableOperator, BigQueryUpdateTableOperator
from airflow.providers.google.cloud.transfers.gcs_to_bigquery import GCSToBigQueryOperator
from airflow.operators.python import PythonOperator
from airflow.providers.google.cloud.sensors.tasks import TaskQueueEmptySensor
from airflow.providers.google.cloud.operators.tasks import CloudTasksQueueCreateOperator, CloudTasksQueueDeleteOperator
from airflow.operators.dummy import DummyOperator


from google.cloud.tasks_v2.types import Queue

logger = logging.getLogger(__name__)

LOD_PRJ = os.environ.get('LOD_PRJ')
BQ_LOCATION = os.environ.get('BQ_LOCATION')
LOD_SA = os.environ.get('LOD_SA')
LOD_BQ_DATASET = os.environ.get('LOD_BQ_DATASET')
REGION = os.environ.get("GCP_REGION")
LOD_GCS_STAGING = os.environ.get("LOD_GCS_STAGING")

workflow_config = json.loads("""${WORKFLOW_CONFIG}""")
config_dag_paramns = workflow_config.get('airflow_dag_config')
config_dag_paramns['default_args'] = {'owner': 'airflow'}
config_dag_paramns.setdefault('dag_id', "api_workflow_${WORKFLOW_KEY}")
config_dag_paramns.setdefault('start_date', datetime.datetime.now() - datetime.timedelta(minutes=1))
config_dag_paramns.setdefault('dag_id','api_workflow_'+workflow_config.get('name'))


api_config = workflow_config.get('api_config')

gcs_to_bigquery = workflow_config.get('airflow_gcs_to_bigquery_config')
gcs_to_bigquery.pop('destination_project_dataset_table',None)
gcs_to_bigquery.setdefault('task_id','gcs_to_bigquery_execute')
gcs_to_bigquery.setdefault('bucket', LOD_GCS_STAGING)
gcs_to_bigquery['bucket'] = gcs_to_bigquery['bucket'].replace('gs://','')

gcs_to_bigquery.setdefault('impersonation_chain', LOD_SA)

workflow_id = workflow_config.get('name')
api_config =  workflow_config.get('api_config')
request_config = api_config.get('request_config',{})

static_headers = request_config.get('static_data',{}).get('headers', {})
static_query_string = request_config.get('static_data',{}).get('query_string', {})
static_body = request_config.get('static_data',{}).get('body', {})

dynamic_headers = request_config.get('dynamic_data',{}).get('headers', {})
dynamic_query_string = request_config.get('dynamic_data',{}).get('query_string', {})
dynamic_body = request_config.get('dynamic_data',{}).get('body', {})

request_config.pop('dynamic_data',None)
request_config.pop('static_data',None)

def get_expiration_time(seconds=84600):
    expiration_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=seconds
    )
    return int(expiration_time.timestamp() * 1000)

def run_bq_job(**kwargs):
    task_instance = kwargs['ti']
    def _json_pairs(alias, dynamic_query_string, static_query_string):
        def _to_query_pairs(items, remove_quotes=False):
            q = '`' if remove_quotes else '"'
            return [f'\'"{key}":"\', {q}{value}{q},\'"\'' for key, value in items.items()]
            
        query_pairs =  _to_query_pairs(dynamic_query_string, True) 
        query_pairs.extend(_to_query_pairs(static_query_string))
        
        json_pairs =  ", ' , ',".join(query_pairs)
       
        return "CONCAT('{',"+json_pairs+",'}') AS "+alias

    secret_name = api_config.get('auth').get('secret_name','none')
    sql_query_secret = '"{}" as secret_name'.format(secret_name)
    sql_query = _json_pairs('query_string', dynamic_query_string, static_query_string)
    sql_body = _json_pairs('body',dynamic_body, static_body)
    sql_headers = _json_pairs('headers',dynamic_headers, static_headers)

    sql_query_source = ', '.join([sql_query_secret, sql_query, sql_body,  sql_headers])

    sql = """
        INSERT INTO `{dataset}.{tmp_log_table}` (
            headers,
            query_string,
            body,
            result,
            exec_time
        )

        WITH query_source AS (
            SELECT
                '{workflow_id}' AS workflow_id,
                '{auth}' AS auth,
                '{request_config}' AS request_config,
                {sql_query_source}    
            FROM `{source_table}`
        )

        SELECT
            headers,
            query_string,
            body,
            TO_JSON_STRING(
                `{dataset}.routine_execute_api_fnc`(
                workflow_id,
                request_config,
                auth,
                headers,
                query_string,
                body,
                '{dataset}.{tmp_result_table}',
                '{queue_name}'
                )
            ) AS result,
            CURRENT_TIMESTAMP() AS exec_time
        FROM query_source
    """.format(dataset=LOD_BQ_DATASET,
        tmp_log_table= task_instance.xcom_pull(task_ids="tmp_log_table", key="bigquery_table")["table_id"],
        workflow_id=workflow_id,
        tmp_result_table=task_instance.xcom_pull(task_ids="tmp_log_table", key="bigquery_table")["table_id"],
        source_table=f"{LOD_PRJ}.{LOD_BQ_DATASET}.lod_ingestion_data_"+task_instance.xcom_pull(task_ids="uuid"),
        queue_name=task_instance.xcom_pull(task_ids="create_queue_gct", key="return_value")["name"],
        auth=json.dumps(api_config.get('auth'),separators=(',', ':')),
        request_config=json.dumps(api_config.get('request_config'),separators=(',', ':')),
        sql_query_source=sql_query_source)
    
    logger.info("generated sql: "+sql)

    start_run_job_in_bq = BigQueryInsertJobOperator(
        task_id='bq_execute_api_fnc',
        gcp_conn_id='bigquery_default',
        project_id=LOD_PRJ,
        location=BQ_LOCATION,
        configuration={
        'jobType':'QUERY',
        'query':{
            "priority": "BATCH",
            'query':sql,
            
            "useLegacySql": False
        }
        },
        impersonation_chain=[LOD_SA]
    )

    start_run_job_in_bq.execute(kwargs)

# Start Declare DAG
with DAG(
    **config_dag_paramns
) as dag:
    
    start = DummyOperator(
        task_id='start',
        trigger_rule='all_success'
    )

    end = DummyOperator(
        task_id='end',
        trigger_rule='all_success'
    )

    #https://cloud.google.com/tasks/docs/creating-queues?hl=en#create_a_queue
    #It can take a few minutes for a newly created queue to be available. We will wait 60 secs
    wait60secs = TimeDeltaSensor(task_id="wait_queue_tobe_available", delta=pendulum.duration(seconds=60))

    generate_uuid = PythonOperator(
        task_id='uuid',
        python_callable=lambda: datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    )

    gcs_to_bigquery_execute = GCSToBigQueryOperator(
        destination_project_dataset_table=f"{LOD_PRJ}.{LOD_BQ_DATASET}.lod_ingestion_data_"+'{{ task_instance.xcom_pull(task_ids="uuid") }}',
        **gcs_to_bigquery
    )

    gcs_to_bigquery_table_expiration = BigQueryUpdateTableOperator(
        task_id="lod_ingestion_data_set_table_exp",
        project_id=LOD_PRJ,
        dataset_id=LOD_BQ_DATASET,
        table_id='lod_ingestion_data_{{ task_instance.xcom_pull(task_ids="uuid") }}',
        fields=["expirationTime"],
        table_resource={
            "expirationTime": get_expiration_time(),
        },
        impersonation_chain=[LOD_SA]
    )

    create_tmp_log_table = BigQueryCreateEmptyTableOperator(
        task_id='tmp_log_table',
        project_id=LOD_PRJ,
        dataset_id=LOD_BQ_DATASET,
        table_id='tbl_process_log_{{ task_instance.xcom_pull(task_ids="uuid") }}',
        location='US', 
        exists_ok=False,
        table_resource={
            "expirationTime": get_expiration_time(),
            "schema": {"fields":[
                {'name': 'headers', 'type': 'STRING', 'mode': 'NULLABLE'},
                {'name': 'query_string', 'type': 'STRING', 'mode': 'NULLABLE'},
                {'name': 'body', 'type': 'STRING', 'mode': 'NULLABLE'},
                {'name': 'result', 'type': 'STRING', 'mode': 'NULLABLE'},
                {'name': 'exec_time', 'type': 'TIMESTAMP', 'mode': 'NULLABLE'}
            ]}
        },
        gcp_conn_id='bigquery_default',
        impersonation_chain=[LOD_SA],
    )

    run_bq_task = PythonOperator(
        task_id='run_bq_task',
        python_callable=run_bq_job,
        provide_context=True
    )

     # Task 1: Create empty table to store result
    create_result_tmp_final_table = BigQueryCreateEmptyTableOperator(
        task_id='tmp_result_table',
        project_id=LOD_PRJ,
        dataset_id=LOD_BQ_DATASET,
        table_id='tbl_result_{{ task_instance.xcom_pull(task_ids="uuid") }}',
        location='US', 
        table_resource={
            "expirationTime": get_expiration_time(),
            "schema": {"fields":[
                {'name': 'request', 'type': 'RECORD', 'mode': 'NULLABLE', 'fields': [
                    {'name': 'uri', 'type': 'STRING', 'mode': 'NULLABLE'},
                    {'name': 'method', 'type': 'STRING', 'mode': 'NULLABLE'},
                    {'name': 'auth_type', 'type': 'STRING', 'mode': 'NULLABLE'},
                    {'name': 'query_string', 'type': 'STRING', 'mode': 'NULLABLE'},
                    {'name': 'body', 'type': 'STRING', 'mode': 'NULLABLE'}
                ]},
                {'name': 'request_time', 'type': 'TIMESTAMP', 'mode': 'NULLABLE'},
                {'name': 'elapsed_time', 'type': 'FLOAT', 'mode': 'NULLABLE'},
                {'name': 'response', 'type': 'RECORD', 'mode': 'NULLABLE', 'fields': [
                    {'name': 'status_code', 'type': 'INTEGER', 'mode': 'NULLABLE'},
                    {'name': 'headers', 'type': 'STRING', 'mode': 'NULLABLE'},
                    {'name': 'body', 'type': 'STRING', 'mode': 'NULLABLE'}
                ]}
            ]
            }
        },
        gcp_conn_id='bigquery_default',
        impersonation_chain=[LOD_SA],
    )

    create_queue = CloudTasksQueueCreateOperator(
        location=REGION,
        project_id=LOD_PRJ,
        task_queue=Queue(),
        queue_name="cloud-task-api-{{ task_instance.xcom_pull(task_ids='uuid') }}",
        task_id="create_queue_gct",
        impersonation_chain=[LOD_SA],
    )

    delete_queue = CloudTasksQueueDeleteOperator(
        location=REGION,
        project_id=LOD_PRJ,
        queue_name="cloud-task-api-{{ task_instance.xcom_pull(task_ids='uuid') }}",
        task_id="delete_queue_gct",
        impersonation_chain=[LOD_SA],
        trigger_rule='all_done'
    )


    # Use TaskQueueEmptySensor to wait until the Cloud Tasks queue is empty
    wait_for_empty_queue = TaskQueueEmptySensor(
        task_id='wait_for_queue_empty',
        project_id=LOD_PRJ,
        location=REGION,
        queue_name="{{ task_instance.xcom_pull(task_ids='create_queue_gct', key='cloud_task_queue')['queue_id'] }}",
        poke_interval=60,  # Check every 60 seconds
        timeout=3600*6,    # Timeout after 6 hour- max time to wait BigQuery
        mode='poke',       # Block the task until the queue is empty
        impersonation_chain=[LOD_SA]
    )

    start >> \
    generate_uuid >> \
    [gcs_to_bigquery_execute, create_result_tmp_final_table,create_tmp_log_table,create_queue] >> \
    wait60secs >> \
    run_bq_task >> \
    wait_for_empty_queue >> \
    delete_queue >> \
    end
    #[delete_queue,gcs_to_bigquery_table_expiration] >> \
    # [delete_queue,delete_result_tmp_final_table] >> \