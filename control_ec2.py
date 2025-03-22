import boto3
from botocore.exceptions import ClientError
import time

def control_ec2_instance(
    instance_id,
    action,
    aws_access_key,
    aws_secret_key,
    region='us-east-1',
    wait_for_completion=True,
    max_wait_seconds=300
):
    """
    Start or stop an EC2 instance.
    
    Args:
        instance_id (str): The ID of the EC2 instance to control
        action (str): 'start' or 'stop'
        aws_access_key (str): AWS Access Key ID
        aws_secret_key (str): AWS Secret Access Key
        region (str): AWS region where the instance is located
        wait_for_completion (bool): Whether to wait for the action to complete
        max_wait_seconds (int): Maximum time to wait for the action to complete
        
    Returns:
        dict: Information about the result of the operation
    """
    # Validate the action parameter
    if action.lower() not in ['start', 'stop']:
        return {
            'success': False,
            'error': "Action must be either 'start' or 'stop'"
        }
    
    # Create EC2 client
    ec2_client = boto3.client(
        'ec2',
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        region_name=region
    )
    
    try:
        # Get current instance state
        response = ec2_client.describe_instances(InstanceIds=[instance_id])
        current_state = response['Reservations'][0]['Instances'][0]['State']['Name']
        
        # Check if the instance is already in the desired state
        if (action.lower() == 'start' and current_state == 'running') or \
           (action.lower() == 'stop' and current_state == 'stopped'):
            return {
                'success': True,
                'message': f"Instance {instance_id} is already {current_state}",
                'instance_id': instance_id,
                'state': current_state
            }
        
        # Perform the requested action
        if action.lower() == 'start':
            response = ec2_client.start_instances(InstanceIds=[instance_id])
            target_state = 'running'
        else:  # stop
            response = ec2_client.stop_instances(InstanceIds=[instance_id])
            target_state = 'stopped'
        
        # Get the status information from the response
        prev_state = response['StartingInstances'][0]['PreviousState']['Name'] if action.lower() == 'start' else \
                     response['StoppingInstances'][0]['PreviousState']['Name']
        
        # If we don't need to wait, return immediately
        if not wait_for_completion:
            return {
                'success': True,
                'message': f"Instance {instance_id} {action} request submitted",
                'instance_id': instance_id,
                'previous_state': prev_state,
                'target_state': target_state
            }
        
        # Wait for the action to complete
        print(f"Waiting for instance {instance_id} to reach '{target_state}' state...")
        wait_start_time = time.time()
        
        while True:
            # Check if we've exceeded the maximum wait time
            if time.time() - wait_start_time > max_wait_seconds:
                return {
                    'success': False,
                    'message': f"Timeout waiting for instance {instance_id} to reach '{target_state}' state",
                    'instance_id': instance_id,
                    'previous_state': prev_state,
                    'current_state': current_state  # Last known state
                }
            
            # Get current instance state
            response = ec2_client.describe_instances(InstanceIds=[instance_id])
            current_state = response['Reservations'][0]['Instances'][0]['State']['Name']
            
            # If we've reached the target state, we're done
            if current_state == target_state:
                return {
                    'success': True,
                    'message': f"Instance {instance_id} is now {current_state}",
                    'instance_id': instance_id,
                    'previous_state': prev_state,
                    'current_state': current_state
                }
            
            # Wait before checking again
            time.sleep(5)
    
    except ClientError as e:
        error_message = e.response['Error']['Message']
        return {
            'success': False,
            'error': f"AWS Error: {error_message}",
            'instance_id': instance_id
        }
    except Exception as e:
        return {
            'success': False,
            'error': f"Error: {str(e)}",
            'instance_id': instance_id
        }
