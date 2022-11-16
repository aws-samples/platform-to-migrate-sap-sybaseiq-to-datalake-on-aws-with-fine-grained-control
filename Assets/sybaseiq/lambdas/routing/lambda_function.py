# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import boto3
import hashlib
import logging
import os
from datetime import datetime

logging.basicConfig(level=logging.INFO)
landing_bucket = os.environ['LandingBucket']
migration_plans_bucket = os.environ['MigrationPlansBucket']
extraction_details_dynamodb_table = os.environ['MigrationDetailsDynamoTable']
extraction_step_funcion_arn = os.environ['MigrationStepFunctionArn']

# Appends JSONs on S3 to the list "jobs"
def append_job(obj, jobs):
    print(obj)
    try:
        j = json.loads(obj.get()['Body'].read().decode('utf-8-sig'))
    except:
        j = json.loads(obj.get()['Body'].read().decode('utf-8'))
               
    if j["Active"]:
        del j["Active"]
        j["OriginFile"] = obj.key
        jobs.append(j)

# Starts the next job inside the JSONs that have multiple jobs to be run in sequence
def start_next_sequential_job(job, stepfunctions_client, extraction_step_funcion_arn, table, extraction_details_dynamodb_table, landing_bucket):
    if len(job["Jobs"]) == 0:
        return 
    
    #When the JSON comes from the Step Function it has an Output Key that should not be sent back to the Step Function
    try:
        del job["Output"]
    except:
        pass
    
    hash_id, name = generate_hash_id_and_name(job["SourceName"], job["Jobs"][0]["SourceTable"], job["Jobs"][0]["MigrationPart"], job["Jobs"][0]["Query"])
    
    job["Query"] = job["Jobs"][0]["Query"] 
    job["NumPartitions"] =job["Jobs"][0]["NumPartitions"]
    job["LowerBound"] =job["Jobs"][0]["LowerBound"]
    job["UpperBound"] = job["Jobs"][0]["UpperBound"] 
    job["JobName"] =job["Jobs"][0]["JobName"]
    job["WorkerType"] = job["Jobs"][0]["WorkerType"] 
    job["NumberOfWorkers"] =job["Jobs"][0]["NumberOfWorkers"] 
    job["MigrationPart"] = job["Jobs"][0]["MigrationPart"]
    job["ExecutionHashId"] = hash_id
    job["SourceTable"] = job["Jobs"][0]["SourceTable"]
    job["ColumnForPartitioningOnS3"] = job["Jobs"][0]["ColumnForPartitioningOnS3"]
    job["ColumnForPartitioningOnSpark"] = job["Jobs"][0]["ColumnForPartitioningOnSpark"]
    job["ExpectedAmountOfRecords"] = job["Jobs"][0]["ExpectedAmountOfRecords"]
    job["CredentialsSecretArn"] = job["Jobs"][0]["CredentialsSecretArn"]
    job["JDBCConnectionString"] = job["Jobs"][0]["JDBCConnectionString"]
    
    del job["Jobs"][0]
    
    job["NumPartitions"] = str(job["NumPartitions"])    
    job["HistoricalDwOnPremiseBucket"] = landing_bucket
    job["HistoricalExtractionTable"] = extraction_details_dynamodb_table 

    if checks_job_run(job, table):
        logging.info(f'This job has been run before: {job["SourceName"]} - {job["SourceTable"]} - {job["MigrationPart"]} - {job["Query"]}')
        start_next_sequential_job(job, stepfunctions_client, extraction_step_funcion_arn, table, extraction_details_dynamodb_table, landing_bucket)
        return 403

    StepFunctionResponse = start_step_function_execution(stepfunctions_client, extraction_step_funcion_arn, name, job)
    
    del job["Jobs"]
    store_execution_details_dynamo(table, job, StepFunctionResponse)
    
    return 

# This function checks the extraction metadata on DynamoDB to check if that extraction has been run before or is running
def checks_job_run(job, table):
    hash_id, name = generate_hash_id_and_name(job["SourceName"], job["SourceTable"], job["MigrationPart"], job["Query"])
    
    response = table.get_item(
        Key={
            "ExecutionHashId": hash_id, "SourceTable": job["SourceTable"]
        },
        AttributesToGet=[
            "GlueJobFinalStatus"
        ]
    )
    
    logging.info(f"checks_job_run: {response}")
    if "Item" in response:
        if response["Item"]["GlueJobFinalStatus"] == "SUCCEEDED" or response["Item"]["GlueJobFinalStatus"] == None or response["Item"]["GlueJobFinalStatus"] == "":
            responseDynamo = table.put_item(
                Item={
                    "ExecutionHashId": f'JobHasRunOrIsRunning-{job["SourceTable"]}-{hash_id}',
                    "SourceTable": job["SourceTable"],
                    "TriedToRunJob": job
                }
            )
            return True
        else:
            return False
    else:
        return False

# This function creates an extraction execution ID and alsol the name for the step function run
def generate_hash_id_and_name(SourceName, SourceTable, MigrationPart, Query):
    code_for_hash = SourceTable+str(MigrationPart)+Query.upper().strip()
    hash_id = ((hashlib.md5(code_for_hash.encode())).hexdigest())
    name = f'{SourceName}-{SourceTable[:28]}-MigrationPart-{str(MigrationPart)[:10]}-{datetime.now().strftime("%Y-%m-%dT%H-%M")}'
    return hash_id, name

# This function start the Step Functions execution
def start_step_function_execution(stepfunctions_client, extraction_step_funcion_arn, name, job):
    StepFunctionResponse = stepfunctions_client.start_execution(
        stateMachineArn=extraction_step_funcion_arn,
        name=name,
        input=json.dumps(job)
    )
    logging.info(f"StepFunctionResponse: {StepFunctionResponse}")
    return StepFunctionResponse

# This function stores the metadata on DynamoDB 
def store_execution_details_dynamo(table, job, StepFunctionResponse):
    del job["HistoricalDwOnPremiseBucket"]
    del job["HistoricalExtractionTable"] 
    del job["CredentialsSecretArn"]

    job["LambdaCallTimestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    job["StateMachineExecutionArn"] = StepFunctionResponse["executionArn"]
    job["StateMachineStartTimestamp"] = StepFunctionResponse["startDate"].strftime("%Y-%m-%d %H:%M:%S")
    job["GlueJobRunId"] = None
    job["GlueJobStartTimestamp"] = None
    job["GlueAmountOfRecords"] = None
    job["GlueFinalTableSchema"] = None
    job["GlueJobEndTimestamp"] = None
    job["GlueJobFinalStatus"] = None
    job["ErrorMessage"] = None
    job["ExecutionTime"] = None
    
    responseDynamo = table.put_item(
        Item=job
    )
    
    logging.info(f"ResponseDynamo: {responseDynamo}")
    del job
    return

#Main Function
def lambda_handler(event, context):
    print(event)
    logging.info('#########################################')
    logging.info(event)
    
    jobs = []
    s3_resource = boto3.resource("s3")
    stepfunctions_client = boto3.client('stepfunctions')
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(extraction_details_dynamodb_table)
    
    # Checks what's invoking this lambda: s3 event, eventbridge, or the step function
    if "Query" in event:
        logging.info('lambda invoked by step function')
        start_next_sequential_job(event, stepfunctions_client, extraction_step_funcion_arn, table, extraction_details_dynamodb_table, landing_bucket)
        return 200 
    
    elif "Records" in event:
        logging.info('lambda invoked by S3 event')
        logging.info(f"s3 event: {event}")
        
        for obj in event["Records"]:
            object = s3_resource.Object(migration_plans_bucket, obj["s3"]["object"]["key"])
            append_job(object, jobs)
        
    else:
        logging.info('lambda invoked by eventbridge')
        bucket = s3_resource.Bucket(migration_plans_bucket)
        objects = list(bucket.objects.filter(Prefix=f"governance/metadata/historical_extraction/sybaseiqdw/run_on_schedule_{event['schedule_number']}/"))
        objects = [o for o in objects if "done" not in o.key and ".json" in o.key]
        
        logging.info("### Objects in the Bucket ###")
        for o in objects:
            logging.info(o.key)
        logging.info("### - ###")
        
        for obj in objects:
            append_job(obj, jobs)
        
        logging.info(f"Amount of Active JSONs in the Bucket: {len(jobs)}")
    
    ################################
    for job in jobs:
        if job["SequentialMultipleParts"]:
            start_next_sequential_job(job, stepfunctions_client, extraction_step_funcion_arn, table, extraction_details_dynamodb_table, landing_bucket)
        
        else:    
            hash_id, name = generate_hash_id_and_name(job["SourceName"], job["SourceTable"], job["MigrationPart"], job["Query"])
            
            job["ExecutionHashId"] = hash_id
            job["NumPartitions"] = str(job["NumPartitions"])
            job["HistoricalDwOnPremiseBucket"] = landing_bucket
            job["HistoricalExtractionTable"] = extraction_details_dynamodb_table 
           
            if checks_job_run(job, table):
                logging.warn(f'This job has been run before: {job["SourceName"]} - {job["SourceTable"]} - {job["MigrationPart"]} - {job["Query"]}')
                return 200
            
            StepFunctionResponse = start_step_function_execution(stepfunctions_client,extraction_step_funcion_arn, name, job)
            store_execution_details_dynamo(table, job, StepFunctionResponse)
        
        
    logging.info('#########################################')
    return 200    
 
