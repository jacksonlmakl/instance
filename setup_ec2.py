import boto3
import paramiko
import time
import os
from botocore.exceptions import ClientError

def execute_commands_on_ec2(
    instance_id,
    aws_access_key,
    aws_secret_key,
    region='us-east-1',
    key_path=None,
    username='ec2-user',
    commands=[],
):
    """
    Connect to an EC2 instance and execute a series of commands.
    
    Args:
        instance_id (str): EC2 instance ID to connect to
        aws_access_key (str): AWS Access Key ID
        aws_secret_key (str): AWS Secret Access Key
        region (str): AWS region where the instance is running
        key_path (str): Path to the private key file for SSH access
        username (str): Username to use for SSH connection (default: ec2-user)
        
    Returns:
        dict: A dictionary containing the command outputs and status
    """
    # Create EC2 client
    ec2_client = boto3.client(
        'ec2',
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        region_name=region
    )
    
    try:
        # Get instance details
        response = ec2_client.describe_instances(InstanceIds=[instance_id])
        instance = response['Reservations'][0]['Instances'][0]
        public_ip = instance.get('PublicIpAddress')
        
        if not public_ip:
            print(f"Instance {instance_id} does not have a public IP address")
            return {"success": False, "error": "No public IP address found"}
        
        if not key_path:
            print("SSH key path not provided")
            return {"success": False, "error": "SSH key path is required"}
        
        if not os.path.exists(key_path):
            print(f"SSH key file not found at {key_path}")
            return {"success": False, "error": f"SSH key file not found at {key_path}"}
        
        # Set up SSH client
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Get instance security status
        instance_status = ec2_client.describe_instance_status(InstanceIds=[instance_id])
        if not instance_status['InstanceStatuses']:
            print(f"Instance {instance_id} may not be running or initialized yet")
            return {"success": False, "error": "Instance not running or initialized"}
        
        # Connect to the instance
        print(f"Connecting to {public_ip} as {username}...")
        
        # Retry connection a few times in case instance is still initializing
        max_retries = 5
        retry_delay = 10  # seconds
        connected = False
        
        for attempt in range(max_retries):
            try:
                ssh.connect(
                    hostname=public_ip,
                    username=username,
                    key_filename=key_path,
                    timeout=20
                )
                connected = True
                break
            except Exception as e:
                print(f"Connection attempt {attempt + 1} failed: {str(e)}")
                if attempt < max_retries - 1:
                    print(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
        
        if not connected:
            print("Failed to connect to the instance after multiple attempts")
            return {"success": False, "error": "Connection failed after multiple attempts"}
        
        
        results = {
            "success": True,
            "commands": []
        }
        
        # Execute commands one by one
        for cmd in commands:
            print(f"Executing: {cmd}")
            stdin, stdout, stderr = ssh.exec_command(cmd)
            exit_status = stdout.channel.recv_exit_status()
            
            # Get command output
            stdout_content = stdout.read().decode('utf-8')
            stderr_content = stderr.read().decode('utf-8')
            
            print(f"Exit status: {exit_status}")
            if stdout_content:
                print(f"STDOUT:\n{stdout_content}")
            if stderr_content:
                print(f"STDERR:\n{stderr_content}")
            
            results["commands"].append({
                "command": cmd,
                "exit_status": exit_status,
                "stdout": stdout_content,
                "stderr": stderr_content
            })
            
            # If a command fails, don't proceed with subsequent commands
            if exit_status != 0:
                print(f"Command failed with exit status {exit_status}")
                results["success"] = False
                break
        
        # Close SSH connection
        ssh.close()
        return results
        
    except ClientError as e:
        print(f"AWS API error: {str(e)}")
        return {"success": False, "error": str(e)}
    except Exception as e:
        print(f"Error: {str(e)}")
        return {"success": False, "error": str(e)}

