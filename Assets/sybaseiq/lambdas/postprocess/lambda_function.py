# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import os
import json
import boto3
import time
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
migration_plans_bucket = os.environ['MigrationPlansBucket']
landing_bucket = os.environ['LandingBucket']
dynamodb_control_table = os.environ['MigrationDetailsDynamoTable']
notification_sns_topic = os.environ['MigrationNotificationsTopic']
glue_database = os.environ['TargetDatabase']
indexcreationtentives = 5
tentativescount = 1

# Function to move successfull or failed extraction files to the respective folder 
def move_processed_files(event, s3_resource, migration_plans_bucket):  
    if event["Output"]["JobRunState"] == "SUCCEEDED":
        logging.info(f"Step Function runned successfully, moving JSONs to 'succeeded/' folder")
        path_split = event["OriginFile"].split("/")
        path_split[-2] = "succeeded"
        dest_path = "/".join(path_split)
        
        
        try:
            copy_source = {
            'Bucket': migration_plans_bucket,
            'Key': event["OriginFile"]
            }
            s3_resource.meta.client.copy(copy_source, migration_plans_bucket, dest_path)
            s3_client = boto3.client('s3')
            response = s3_client.delete_object(
                Bucket=migration_plans_bucket,
                Key=event["OriginFile"],
                )
            
            logging.info(f"s3_response: {response}")
        except Exception as e:
            logging.info(f"It was not possible to move the object. Maybe it has already been moved.")
            logging.warn(f"Exception: {e}")
    else:
        logging.info(f"Step Function DID NOT run successfully, moving JSONs to 'failed/' folder")
        #try:
        path_split = event["OriginFile"].split("/")
        print(path_split)
        
        path_split[-2] = "failed"
        
        dest_path = "/".join(path_split)
        
        logging.info(f"dest_path: {dest_path}")
        
        try:
            copy_source = {
            'Bucket': migration_plans_bucket,
            'Key': event["OriginFile"]
            }
            s3_resource.meta.client.copy(copy_source, migration_plans_bucket, dest_path)
            s3_client = boto3.client('s3')
            response = s3_client.delete_object(
                Bucket=migration_plans_bucket,
                Key=event["OriginFile"],
                )
            
            logging.info(f"s3_response: {response}")
        except Exception as e:
            logging.info(f"It was not possible to move the object. Maybe it has already been moved.")
            logging.warn(f"Exception: {e}")
        
# Function to compare the amount of records expected (this is defined on the extraction config JSON) and the amunt of records that AWS Glue pulled from the database
# If the ExpectedAmountOfRecords is empty on the JSON the function returns the first variable as False 
def compare_amount_of_records(event, table):
    if event["ExpectedAmountOfRecords"] != " ":
        try:
            response = table.get_item(
                Key={
                    "ExecutionHashId": event["ExecutionHashId"], "SourceTable": event["SourceTable"]
                },
                AttributesToGet=[
                    "GlueAmountOfRecords"
                ]
            )
            GlueAmountOfRecords = response["Item"]["GlueAmountOfRecords"]
            if int(event["ExpectedAmountOfRecords"]) == int(GlueAmountOfRecords):
                return True, True, GlueAmountOfRecords
            else:
                return True, False, GlueAmountOfRecords
        
        except Exception as e:
            logging.warn("It was not possible to compare the amount of records")
            logging.warn(e)
            return True, False, "Not found"
    else:
        return False, False, ""

# This function formats and publishes on an Amazon Simple Notification Service (SNS) topic, then SNS sends an e-mail to the subscribers
def sns_publish(event, comp_amount_records, result_comp_amount_records, GlueAmountOfRecords, sns_client, notification_sns_topic):
    if comp_amount_records:
        if result_comp_amount_records:
            subject = f'[Historical Extraction] - {event["Output"]["JobRunState"]} - {event["SourceName"]} {event["SourceTable"]} Migration Part {event["MigrationPart"]}'
            try:
                message = {
                    "FinalStatus": event["Output"]["JobRunState"],
                    "ExecutionHashId": event["ExecutionHashId"],
                    "CompareExpectedAmountOfRecords": f'OK - Expected {event["ExpectedAmountOfRecords"]} and got {GlueAmountOfRecords}',
                    "GlueJobRunId": event["Output"]["JobRunId"],
                    "ExecutionTime": str(timedelta(seconds=event["Output"]["ExecutionTime"])),
                    "Query": event["Query"],
                    "JSONInput": event
                }
            except Exception as e:
                logging.warn(e)
                message = {"JSONInput": event}
        
        else:
            subject = f'[Historical Extraction] - {event["Output"]["JobRunState"]} WITH WARNING - {event["SourceName"]} {event["SourceTable"]} Migration Part {event["MigrationPart"]}'
            try:
                message = {
                    "FinalStatus": event["Output"]["JobRunState"],
                    "ExecutionHashId": event["ExecutionHashId"],
                    "CompareExpectedAmountOfRecords": f'NOT OK - Expected {event["ExpectedAmountOfRecords"]} and got {GlueAmountOfRecords}',
                    "GlueJobRunId": event["Output"]["JobRunId"],
                    "ExecutionTime": str(timedelta(seconds=event["Output"]["ExecutionTime"])),
                    "Query": event["Query"],
                    "JSONInput": event
                }
            except Exception as e:
                logging.warn(e)
                message = {"JSONInput": event}
    
    else:
        subject = f'[Historical Extraction] - {event["Output"]["JobRunState"]} - {event["SourceName"]} {event["SourceTable"]} Migration Part {event["MigrationPart"]}'
        try:
            message = {
                "FinalStatus": event["Output"]["JobRunState"],
                "ExecutionHashId": event["ExecutionHashId"],
                "GlueJobRunId": event["Output"]["JobRunId"],
                "ExecutionTime": str(timedelta(seconds=event["Output"]["ExecutionTime"])),
                "Query": event["Query"],
                "JSONInput": event
            }
        except Exception as e:
                logging.warn(e)
                message = {"JSONInput": event}

    logging.info(f"subject: {subject}")
    
    response = sns_client.publish(
        TopicArn=notification_sns_topic,
        Message=json.dumps({"default": json.dumps(message)}),
        #Message=message,
        Subject=str(subject)[:99],
        MessageStructure='json'
    )

    return

# This function creates a partition index on AWS Glue Data Catalog if the index does not exists
# An exponential back-off was implemented on the API call
def create_partition_index(event):
    table_name = f"{event['SourceName']}_{event['SourceTable']}"

    glue_client = boto3.client("glue")
    response = glue_client.get_table(
        DatabaseName=glue_database,
        Name=table_name
    )

    partition_keys = []
    
    for column in response['Table']['PartitionKeys']:
        partition_keys.append(column['Name'])
        
    logging.info(f"partition_keys: {partition_keys}")

    if len(partition_keys) > 0:
        logging.info(f"partition_keys: {partition_keys}")
    else:
        logging.info(f"Table with no partition keys, therefore no index can be created")
        return

    try:
        logging.info("Updating Glue Data Catalog index partitions for table " + event['SourceTable'] + " in Glue database db_dwonpremise")
        index_creation = glue_client.create_partition_index(
            DatabaseName = glue_database,
            TableName = table_name,
            PartitionIndex={
                'Keys': partition_keys,
                'IndexName': "-".join(partition_keys)
                }
            )
        logging.info("Glue Data Catalog partition index created for table " + event['SourceTable'] + " in Glue database db_dwonpremise")
    except Exception as e:
        if 'AlreadyExistsException' in str(e):
            logging.info("Glue Data Catalog partition index already exists for table " + event['SourceTable'] + " in Glue database db_dwonpremise")
        else:
            tentativescount = 1
            while tentativescount <= indexcreationtentives:
                logging.info("Tentative " + str(tentativescount) + " to create partition index in Glue Data Catalog for table " + event['SourceTable'] + " in Glue database db_dwonpremise")
                try:
                    time.sleep(tentativescount*10)
                    index_creation = glue_client.create_partition_index(
                        DatabaseName = glue_database,
                        TableName = table_name,
                        PartitionIndex={
                            'Keys': partition_keys,
                            'IndexName': "-".join(partition_keys)
                            }
                        )
                    tentativescount = indexcreationtentives + 1
                    logging.info("Glue Data Catalog partition index created for table " + event['SourceTable'] + " in Glue database db_dwonpremise")
                except Exception as e:
                    tentativescount +=1
                    if tentativescount > indexcreationtentives:
                        logging.warn("An error occurred trying to create partition index in Glue Data Catalog for table " + event['SourceTable'] + " in Glue database db_dwonpremise")
                        logging.error(e)
                        return 0


#Main Function
def lambda_handler(event, context):
    logging.info('#########################################')
    print(event)
    
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(dynamodb_control_table)
    
    s3_resource = boto3.resource("s3")
    bucket = s3_resource.Bucket(migration_plans_bucket)
    
    sns_client = boto3.client('sns')

    comp_amount_records, result_comp_amount_records, GlueAmountOfRecords = compare_amount_of_records(event, table)
    
    logging.info(f"comp_amount_records: {comp_amount_records}")
    logging.info(f"result_comp_amount_records: {result_comp_amount_records}")
    logging.info(f"GlueAmountOfRecords: {GlueAmountOfRecords}")
    
    sns_publish(event, comp_amount_records, result_comp_amount_records, GlueAmountOfRecords, sns_client, notification_sns_topic)
    
    move_processed_files(event, s3_resource, migration_plans_bucket)
    
    if (event["ColumnForPartitioningOnS3"] != " " or event["ColumnForPartitioningOnS3"] != "" or event["ColumnForPartitioningOnS3"] != None) and event["Output"]["JobRunState"] == "SUCCEEDED":
        create_partition_index(event)

    logging.info('#########################################')
    return 200    
 
