# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from awsglue.context import GlueContext
from awsglue.dynamicframe import DynamicFrame
from pyspark.sql import SparkSession
from pyspark.sql.functions import *
from pyspark import SparkContext
import boto3
import base64
import re
from botocore.exceptions import ClientError
import json
import sys
from datetime import datetime
import time

# Setting up variables from the Glue Arguments
args = getResolvedOptions(sys.argv, ["SourceName", "SourceDatabase","TargetDatabase", "SourceSchema", "SourceTable", \
    "Query","NumPartitions", "LowerBound", "UpperBound","ExecutionHashId", "HistoricalDwOnPremiseBucket",\
    "HistoricalExtractionTable", "ColumnForPartitioningOnS3", "ColumnForPartitioningOnSpark", \
    "CredentialsSecretArn", "JDBCConnectionString"])
SourceName = args["SourceName"]
SourceDatabase = args["SourceDatabase"]
TargetDatabase = args["TargetDatabase"]
SourceSchema = args["SourceSchema"]
SourceTable = args["SourceTable"]
Query = args["Query"]
NumPartitions = args["NumPartitions"]
LowerBound = args["LowerBound"]
UpperBound = args["UpperBound"]
ExecutionHashId = args["ExecutionHashId"]
HistoricalDwOnPremiseBucket = args["HistoricalDwOnPremiseBucket"]
HistoricalExtractionTable = args["HistoricalExtractionTable"]
ColumnForPartitioningOnS3 = args["ColumnForPartitioningOnS3"]
ColumnForPartitioningOnSpark = args["ColumnForPartitioningOnSpark"]
CredentialsSecretArn = args["CredentialsSecretArn"]
JDBCConnectionString = args["JDBCConnectionString"]

load_timestamp = datetime.utcnow()

# Setting up boto3 clients and components 
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(HistoricalExtractionTable)
s3_client = boto3.client('s3')
s3_resource = boto3.resource("s3")
bucket = s3_resource.Bucket(HistoricalDwOnPremiseBucket)

# Creating Spark and Glue context and initiating logs 
sc = SparkContext()
glueContext = GlueContext(sc)
logger = glueContext.get_logger()
logger.info(f"Starting extraction for table {SourceTable}")

# Function to get the User and Password from AWS Secrets Manager
def get_secret():
    secret_name = CredentialsSecretArn
    region_name = "us-east-1"

    # Create a Secrets Manager client
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name=region_name
    )
    try:
        get_secret_value_response = client.get_secret_value(SecretId=secret_name)
    
    except ClientError as e:
        if e.response['Error']['Code'] == 'DecryptionFailureException':
            logger.error(f"Secrets Manager can't decrypt the protected secret text using the provided KMS key. e: {e}")
            raise e
        elif e.response['Error']['Code'] == 'InternalServiceErrorException':
            logger.error(f"An error occurred on the server side. e: {e}")
            raise e
        elif e.response['Error']['Code'] == 'InvalidParameterException':
            logger.error(f"You provided an invalid value for a parameter. e: {e}")
            raise e
        elif e.response['Error']['Code'] == 'InvalidRequestException':
            logger.error(f"You provided a parameter value that is not valid for the current state of the resource. e: {e}")
            raise e
        elif e.response['Error']['Code'] == 'ResourceNotFoundException':
            logger.error(f"We can't find the resource that you asked for. e: {e}")
            raise e
    else:
        # Decrypts secret using the associated KMS key.
        # Depending on whether the secret is a string or binary, one of these fields will be populated.
        if 'SecretString' in get_secret_value_response:
            secret = get_secret_value_response['SecretString']
            return secret
        else:
            decoded_binary_secret = base64.b64decode(get_secret_value_response['SecretBinary'])
            return decoded_binary_secret

# Sometimes blank spaces are added to string columns during the conversion to Spark datatypes
# This function trims left and right blank spaces on all the string columns
def trim_string_columns(df):
    string_columns = []
    for col in df.dtypes:
        if (col[1] == "string" or col[1] == "StringType") and col[0] != "migration_ExecutionHashId":
            string_columns.append(col[0])

    logger.info(f"string_columns: {string_columns}")

    for column in string_columns:
        df = df.withColumn(column,ltrim(column))\
                .withColumn(column,rtrim(column))
    
    return df

# This function returns the name of the column transformed to be compatible with AWS Glue Data Catalog and Amazon Athena
def fix_col_names(s):
    return re.sub("[^A-Za-z\d_]","_",s.lower().strip())

# Function responsable for saving the data to S3 according to be specified partitions. It also updates the AWS Glue Data Catalog
def save_to_s3(jdbcDF, numRecords):
    for c in jdbcDF.columns:
        jdbcDF = jdbcDF.withColumnRenamed(c, fix_col_names(c))

    if ColumnForPartitioningOnS3 in [" ","",None]:
        
        df = jdbcDF.withColumn("migration_tp_utc", lit(load_timestamp)) \
                    .withColumn("migration_ExecutionHashId", lit(ExecutionHashId))
        
        df = trim_string_columns(df)

        final_schema = df._jdf.schema().treeString()
        logger.info(f"final_schema: {final_schema}")
        
        '''
        Possibility to overwrite the files in a specific partition to avoid duplication, however it can 
        cause data loss (Glue Data Sink only appends, so a manual solution is needed to overwrite a partition). 
        If the column you use on the WHERE condition on the query is different from the "ColumnForPartitioningOnS3" then DO NOT use this
        '''
        # objects = list(bucket.objects.filter(Prefix=f"{SourceName}/{SourceSchema}/{SourceTable}/"))
        # if len(objects) > 0:
        #     logger.info(f"Partition {SourceName}/{SourceSchema}/{SourceTable}/ already exists")
        #     for obj in objects:
        #         logger.info(f"Deleting {obj.key}")
        #         response = s3_client.delete_object(
        #             Bucket=HistoricalDwOnPremiseBucket,
        #             Key=obj.key
        #         )
        # else:
        #     logger.info(f"Location {SourceName}/{SourceSchema}/{SourceTable} do not exists")
        
        if numRecords < 50000000: #50 million rows
            df = df.coalesce(8) 
        elif numRecords >= 50000000 and numRecords < 100000000: #Between 50 million and 100 million rows
            df = df.coalesce(16) 
        else:
            df = df.coalesce(24) 
        
        glue_df = DynamicFrame.fromDF(df, glueContext, "glue_df")
        
        sink = glueContext.getSink(
            connection_type="s3", 
            path=f"s3://{HistoricalDwOnPremiseBucket}/{SourceName}/{SourceSchema}/{SourceTable}/",
            enableUpdateCatalog=True)
        sink.setFormat("glueparquet")
        sink.setCatalogInfo(catalogDatabase=TargetDatabase, catalogTableName=f"{SourceName}_{SourceTable}")
        sink.writeFrame(glue_df)
        
        return final_schema
    else:
        ColumnForPartitioningOnS3DataType = str(jdbcDF.schema[ColumnForPartitioningOnS3].dataType)
        logger.info(f"ColumnForPartitioningOnS3DataType: {ColumnForPartitioningOnS3DataType}")
        
        if ColumnForPartitioningOnS3DataType in ["DateType","TimestampType"]: 
            df = jdbcDF.withColumn("year", date_format(col(ColumnForPartitioningOnS3), "y")) \
                    .withColumn("month", date_format(col(ColumnForPartitioningOnS3), "M")) \
                    .withColumn("day", date_format(col(ColumnForPartitioningOnS3), "d")) \
                    .withColumn("migration_tp_utc", lit(load_timestamp)) \
                    .withColumn("migration_ExecutionHashId", lit(ExecutionHashId))    

            df = trim_string_columns(df)

            final_schema = df._jdf.schema().treeString()
            logger.info(f"final_schema: {final_schema}")
            
            '''
            Possibility to overwrite the files in a specific partition to avoid duplication, however it can 
            cause data loss (Glue Data Sink only appends, so a manual solution is needed to overwrite a partition). 
            If the column you use on the WHERE condition on the query is different from the "ColumnForPartitioningOnS3" then DO NOT use this
            '''
            #Checks if partition already exists on S3. If it exists it is deleted
            # df_to_list_s3 = df.select(col('year'), col('month'), col('day')).distinct().collect()
            # for row in df_to_list_s3:
            #     s3_prefix = f"{SourceName}/{SourceSchema}/{SourceTable}/year={row[0]}/month={row[1]}/day={row[2]}/"
            #     print(s3_prefix)
                
            #     objects = list(bucket.objects.filter(Prefix=str(s3_prefix)))
            #     if len(objects) > 0:
            #         logger.info(f"Partition {s3_prefix} already exists")
            #         for obj in objects:
            #             logger.info(f"Deleting {obj.key}")
            #             response = s3_client.delete_object(
            #                 Bucket=HistoricalDwOnPremiseBucket,
            #                 Key=obj.key
            #             )
            #     else:
            #         logger.info(f"Partition {s3_prefix} does not exist")
        else:
            df = jdbcDF.withColumn("migration_tp_utc", lit(load_timestamp)) \
                    .withColumn("migration_ExecutionHashId", lit(ExecutionHashId))

            df = trim_string_columns(df)

            final_schema = df._jdf.schema().treeString()
            logger.info(f"final_schema: {final_schema}")
            
            '''
            Possibility to overwrite the files in a specific partition to avoid duplication, however it can 
            cause data loss (Glue Data Sink only appends, so a manual solution is needed to overwrite a partition). 
            If the column you use on the WHERE condition on the query is different from the "ColumnForPartitioningOnS3" then DO NOT use this
            '''
            #Checks if partition already exists on S3. If it exists it is deleted
            # df_to_list_s3 = df.select(col(ColumnForPartitioningOnS3)).distinct().collect()
            # for row in df_to_list_s3:
            #     s3_prefix = f"{SourceName}/{SourceSchema}/{SourceTable}/{ColumnForPartitioningOnS3}={row[0]}/"
            #     logger.info(f"s3_prefix = {s3_prefix}")
                
            #     objects = list(bucket.objects.filter(Prefix=str(s3_prefix)))
            #     if len(objects) > 0:
            #         logger.info(f"Partition {s3_prefix} already exists")
            #         for obj in objects:
            #             logger.info(f"Deleting {obj.key}")
            #             response = s3_client.delete_object(
            #                 Bucket=HistoricalDwOnPremiseBucket,
            #                 Key=obj.key
            #             )
            #     else:
            #         logger.info(f"Partition {s3_prefix} does not exist")
               
        glue_df = DynamicFrame.fromDF(df, glueContext, "glue_df")
        
        if ColumnForPartitioningOnS3DataType == "DateType" or ColumnForPartitioningOnS3DataType == "TimestampType":
            sink = glueContext.getSink(
                connection_type="s3", 
                path=f"s3://{HistoricalDwOnPremiseBucket}/{SourceName}/{SourceSchema}/{SourceTable}/",
                enableUpdateCatalog=True,
                partitionKeys=["year","month","day"])
        else:
            sink = glueContext.getSink(
                connection_type="s3", 
                path=f"s3://{HistoricalDwOnPremiseBucket}/{SourceName}/{SourceSchema}/{SourceTable}/",
                enableUpdateCatalog=True,
                partitionKeys=[f"{ColumnForPartitioningOnS3}"])


        sink.setFormat("glueparquet")
        sink.setCatalogInfo(catalogDatabase=TargetDatabase, catalogTableName=f"{SourceName}_{SourceTable}")
        sink.writeFrame(glue_df)
        
        return final_schema

def main():
    # Wait the ENI to be provisioned 
    time.sleep(15)
    
    # Updates DynamoDB table with metadata about the extraction
    responseDynamo = table.update_item(
        Key={
            'ExecutionHashId': ExecutionHashId,
            'SourceTable': SourceTable
        },
        UpdateExpression="set GlueJobStartTimestamp=:v1",
        ExpressionAttributeValues={
            ':v1': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    )

    # logger.info(f"responseDynamo: {responseDynamo}")
    secret = json.loads(get_secret())
    logger.info('Secret was successfully retrieved')

    # Building a spark session with SybaseIQ's JDBC driver
    spark = SparkSession.builder.appName("sybase_migration").\
            config("spark.driver.extraClassPath","/tmp/sybaseiq_jconn4.jar").getOrCreate()

    if ColumnForPartitioningOnSpark not in [" ",""]:
        jdbcDF = spark.read.format("jdbc") \
            .option("url",f"{JDBCConnectionString}") \
            .option("dbtable", f"({Query}) as q") \
            .option("USER", secret["user"]) \
            .option("PASSWORD", secret["password"]) \
            .option("partitionColumn", ColumnForPartitioningOnSpark) \
            .option("LowerBound", LowerBound) \
            .option("UpperBound", UpperBound) \
            .option("NumPartitions", NumPartitions) \
            .load()
        
        jdbcDF.cache()
        df_count = jdbcDF.count()
        logger.info(f"DF Count: {df_count}")
        
        # Updates DynamoDB table with metadata about the extraction
        responseDynamo = table.update_item(
            Key={
                'ExecutionHashId': ExecutionHashId,
                'SourceTable': SourceTable
            },
            UpdateExpression="set GlueAmountOfRecords=:v1",
            ExpressionAttributeValues={
                ':v1': df_count
            }
        )
        logger.info(f"responseDynamo: {responseDynamo}")
        
        final_schema = save_to_s3(jdbcDF, df_count)

    else:
        jdbcDF = spark.read.format("jdbc") \
            .option("url",f"{JDBCConnectionString}") \
            .option("Query", Query) \
            .option("USER", secret["user"]) \
            .option("PASSWORD", secret["password"]) \
            .load()
        
        jdbcDF.cache()
        df_count = jdbcDF.count()
        logger.info(f"DF Count: {df_count}")
        
        # Updates DynamoDB table with metadata about the extraction
        responseDynamo = table.update_item(
            Key={
                'ExecutionHashId': ExecutionHashId,
                'SourceTable': SourceTable
            },
            UpdateExpression="set GlueAmountOfRecords=:v1",
            ExpressionAttributeValues={
                ':v1': df_count
            }
        )
        logger.info(f"responseDynamo: {responseDynamo}")
        
        final_schema = save_to_s3(jdbcDF, df_count)

    # Updates DynamoDB table with metadata about the extraction
    responseDynamo = table.update_item(
            Key={
                'ExecutionHashId': ExecutionHashId,
                'SourceTable': SourceTable
            },
            UpdateExpression="set GlueJobEndTimestamp=:v1, GlueFinalTableSchema=:v2",
            ExpressionAttributeValues={
                ':v1': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ':v2': final_schema
            }
        )
    logger.info(f"responseDynamo: {responseDynamo}")


if __name__ == "__main__":
    main()
