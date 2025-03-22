import boto3

def create_ec2_instance(access_key, secret_key, region='us-east-1', 
                        launch_template_id='lt-0a9087a5f5029d57c', 
                        instance_count=1):
    """
    Create a new EC2 instance using a specified launch template.
    
    Args:
        access_key (str): AWS Access Key ID
        secret_key (str): AWS Secret Access Key
        region (str): AWS region name (default: 'us-east-1')
        launch_template_id (str): Launch Template ID to use
        instance_count (int): Number of instances to launch (default: 1)
        
    Returns:
        dict: Response from AWS containing instance details
    """
    # Create an EC2 client with explicit credentials
    ec2_client = boto3.client(
        'ec2',
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region
    )
    
    # Launch the instance using the template
    response = ec2_client.run_instances(
        LaunchTemplate={
            'LaunchTemplateId': launch_template_id,
            'Version': '$Latest'  # Use the latest version of the template
        },
        MinCount=instance_count,
        MaxCount=instance_count
    )
    
    # Extract and return instance IDs
    instance_ids = [instance['InstanceId'] for instance in response['Instances']]
    
    print(f"Successfully launched {len(instance_ids)} instance(s):")
    for idx, instance_id in enumerate(instance_ids, 1):
        print(f"  {idx}. {instance_id}")
    
    return response
