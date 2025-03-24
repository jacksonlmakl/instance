import boto3
import time
import os
from dotenv import load_dotenv

class EC2Manager:
    """
    A class to manage EC2 instances including creation, setup, launching, starting, and stopping.
    """
    
    def __init__(self, access_key=None, secret_key=None, region=None, 
                 launch_template_id=None, ssh_key_path=None, ssh_username=None, instance_id=None):
        """
        Initialize the EC2Manager with credentials and configuration.
        
        If any parameters are None, it will attempt to load them from environment variables.
        
        Args:
            access_key (str): AWS Access Key ID
            secret_key (str): AWS Secret Access Key
            region (str): AWS region
            launch_template_id (str): Launch template ID for creating instances
            ssh_key_path (str): Path to SSH key file
            ssh_username (str): Username for SSH connection
            instance_id (str): Optional ID of an existing EC2 instance to manage
        """
        # Load environment variables if not explicitly provided
        load_dotenv()
        
        self.access_key = access_key or os.getenv("AWS_ACCESS_KEY")
        self.secret_key = secret_key or os.getenv("AWS_SECRET_KEY")
        self.region = region or os.getenv("AWS_REGION")
        self.launch_template_id = launch_template_id or os.getenv("LAUNCH_TEMPLATE_ID")
        self.ssh_key_path = ssh_key_path or os.getenv("SSH_KEY_PATH")
        self.ssh_username = ssh_username or os.getenv("SSH_USERNAME")
        self.instance_id = instance_id or os.getenv("INSTANCE_ID")
        

        # Validate essential parameters
        missing_params = []
        for param_name, param_value in [
            ("access_key", self.access_key),
            ("secret_key", self.secret_key),
            ("region", self.region)
        ]:
            if not param_value:
                missing_params.append(param_name)
        
        if missing_params:
            raise ValueError(f"Missing required parameters: {', '.join(missing_params)}")
        
        # Create a reusable EC2 client
        self.ec2_client = boto3.client(
            'ec2',
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region
        )
        #Get Public IP Address
        if self.instance_id != None:
            response = self.ec2_client.describe_instances(InstanceIds=[self.instance_id])
            self.public_ip = response['Reservations'][0]['Instances'][0].get('PublicIpAddress')
            self.instance_url = f"http://{self.public_ip}:1100" if self.public_ip else None
    def create(self, wait_seconds=60):
        """
        Create a new EC2 instance using the specified launch template.
        Sets the instance_id property of the class to the newly created instance.
        
        Args:
            wait_seconds (int): Seconds to wait after instance creation before returning
            
        Returns:
            str: The ID of the created instance
        """
        if not self.launch_template_id:
            raise ValueError("launch_template_id is required to create an instance")
        
        try:
            # Launch the instance using the template
            response = self.ec2_client.run_instances(
                LaunchTemplate={
                    'LaunchTemplateId': self.launch_template_id,
                    'Version': '$Latest'  # Use the latest version of the template
                },
                MinCount=1,
                MaxCount=1
            )
            
            # Extract instance ID and store it in the class
            self.instance_id = response['Instances'][0]['InstanceId']
            print(f"Created instance with ID: {self.instance_id}")
            
            # Wait for the instance to initialize
            print(f"Waiting {wait_seconds} seconds for the instance to initialize...")
            time.sleep(wait_seconds)
            response = self.ec2_client.describe_instances(InstanceIds=[self.instance_id])
            self.public_ip = response['Reservations'][0]['Instances'][0].get('PublicIpAddress')
            self.instance_url = f"http://{self.public_ip}:1100" if self.public_ip else None
            return self.instance_id
            
        except Exception as e:
            print(f"Error creating instance: {str(e)}")
            raise
    
    def setup(self, instance_id=None):
        """
        Set up an EC2 instance by installing required software.
        
        Args:
            instance_id (str): ID of the instance to set up. If None, uses the instance ID stored in the class.
            
        Returns:
            dict: Result of the setup operation
        """
        instance_id = instance_id or self.instance_id
        if not instance_id:
            raise ValueError("No instance ID available. Either provide an instance_id parameter or run create() first.")
        
        if not self.ssh_key_path:
            raise ValueError("ssh_key_path is required for setup")
        
        if not self.ssh_username:
            raise ValueError("ssh_username is required for setup")
        
        try:
            result = self._execute_commands(
                instance_id=instance_id,
                commands=["git clone https://github.com/jacksonlmakl/manager.git && cd manager && bash bin/install"]
            )
            
            print("\nSetup execution summary:")
            print(f"Overall success: {result['success']}")
            
            return result
            
        except Exception as e:
            print(f"Error during setup: {str(e)}")
            raise
    
    def launch(self, instance_id=None):
        """
        Launch the application on an EC2 instance.
        
        Args:
            instance_id (str): ID of the instance to launch the app on. If None, uses the instance ID stored in the class.
            
        Returns:
            dict: Result of the launch operation
        """
        instance_id = instance_id or self.instance_id
        if not instance_id:
            raise ValueError("No instance ID available. Either provide an instance_id parameter or run create() first.")
        
        try:
            result = self._execute_commands(
                instance_id=instance_id,
                commands=["""cd manager && (bash ~/manager/launch > ~/launch.log 2>&1 &) < /dev/null ""","""cd manager && (bash ~/manager/stop > ~/stop.log 2>&1 &) < /dev/null ""","""cd manager && (bash ~/manager/start > ~/start.log 2>&1 &) < /dev/null """]
            )
            
            print("\nLaunch execution summary:")
            print(f"Overall success: {result['success']}")
            
            return result
            
        except Exception as e:
            print(f"Error during launch: {str(e)}")
            raise
    
    def stop(self, instance_id=None, wait_for_completion=True):
        """
        Stop an EC2 instance.
        
        Args:
            instance_id (str): ID of the instance to stop. If None, uses the instance ID stored in the class.
            wait_for_completion (bool): Whether to wait for the instance to fully stop
            
        Returns:
            dict: Result of the stop operation
        """
        instance_id = instance_id or self.instance_id
        if not instance_id:
            raise ValueError("No instance ID available. Either provide an instance_id parameter or run create() first.")
        
        try:
            # Get current instance state
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
            current_state = response['Reservations'][0]['Instances'][0]['State']['Name']
            
            # Check if already stopped
            if current_state == 'stopped':
                return {
                    'success': True,
                    'message': f"Instance {instance_id} is already stopped",
                    'instance_id': instance_id,
                    'state': current_state
                }
            
            # Stop the instance
            response = self.ec2_client.stop_instances(InstanceIds=[instance_id])
            print(f"Stopping instance {instance_id}...")
            
            # If we don't need to wait, return immediately
            if not wait_for_completion:
                return {
                    'success': True,
                    'message': f"Instance {instance_id} stop request submitted",
                    'instance_id': instance_id,
                    'previous_state': response['StoppingInstances'][0]['PreviousState']['Name']
                }
            
            # Wait for the instance to stop
            waiter = self.ec2_client.get_waiter('instance_stopped')
            waiter.wait(InstanceIds=[instance_id])
            
            return {
                'success': True,
                'message': f"Instance {instance_id} is now stopped",
                'instance_id': instance_id,
                'previous_state': response['StoppingInstances'][0]['PreviousState']['Name'],
                'current_state': 'stopped'
            }
            
        except Exception as e:
            print(f"Error stopping instance: {str(e)}")
            return {
                'success': False,
                'error': str(e),
                'instance_id': instance_id
            }
    
    def start(self, instance_id=None, wait_for_completion=True):
        """
        Start an EC2 instance.
        
        Args:
            instance_id (str): ID of the instance to start. If None, uses the instance ID stored in the class.
            wait_for_completion (bool): Whether to wait for the instance to fully start
            
        Returns:
            dict: Result of the start operation
        """
        instance_id = instance_id or self.instance_id
        if not instance_id:
            raise ValueError("No instance ID available. Either provide an instance_id parameter or run create() first.")
        
        try:
            # Get current instance state
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
            current_state = response['Reservations'][0]['Instances'][0]['State']['Name']
            
            # Check if already running
            if current_state == 'running':
                return {
                    'success': True,
                    'message': f"Instance {instance_id} is already running",
                    'instance_id': instance_id,
                    'state': current_state
                }
            
            # Start the instance
            response = self.ec2_client.start_instances(InstanceIds=[instance_id])
            print(f"Starting instance {instance_id}...")
            
            # If we don't need to wait, return immediately
            if not wait_for_completion:
                return {
                    'success': True,
                    'message': f"Instance {instance_id} start request submitted",
                    'instance_id': instance_id,
                    'previous_state': response['StartingInstances'][0]['PreviousState']['Name']
                }
            
            # Wait for the instance to start
            waiter = self.ec2_client.get_waiter('instance_running')
            waiter.wait(InstanceIds=[instance_id])
            
            # Wait a bit more for the instance to fully initialize
            time.sleep(30)
            
            return {
                'success': True,
                'message': f"Instance {instance_id} is now running",
                'instance_id': instance_id,
                'previous_state': response['StartingInstances'][0]['PreviousState']['Name'],
                'current_state': 'running'
            }
            
        except Exception as e:
            print(f"Error starting instance: {str(e)}")
            return {
                'success': False,
                'error': str(e),
                'instance_id': instance_id
            }
    
    def check_log(self, instance_id=None, log_file_path='~/launch.log', lines=50):
        """
        Check the log file of a running application on an EC2 instance.
        
        Args:
            instance_id (str): ID of the instance to check logs on. If None, uses the instance ID stored in the class.
            log_file_path (str): Path to the log file on the instance
            lines (int): Number of lines to retrieve from the end of the log
            
        Returns:
            str: Content of the log file
        """
        instance_id = instance_id or self.instance_id
        if not instance_id:
            raise ValueError("No instance ID available. Either provide an instance_id parameter or run create() first.")
        
        try:
            import paramiko
            
            # Get instance details
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
            instance = response['Reservations'][0]['Instances'][0]
            public_ip = instance.get('PublicIpAddress')
            
            if not public_ip:
                return "Instance does not have a public IP address"
            
            # Set up SSH client
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            # Connect to the instance
            try:
                ssh.connect(
                    hostname=public_ip,
                    username=self.ssh_username,
                    key_filename=self.ssh_key_path,
                    timeout=20
                )
            except Exception as e:
                return f"Failed to connect to the instance: {str(e)}"
            
            # Check if the process is still running
            stdin, stdout, stderr = ssh.exec_command(f"pgrep -f 'cd manager && bash ~/manager/launch'")
            process_ids = stdout.read().decode('utf-8').strip()
            
            process_status = "Process status: "
            if process_ids:
                process_status += f"Running (PID: {process_ids})"
            else:
                process_status += "Not running"
            
            # Get the log file content
            stdin, stdout, stderr = ssh.exec_command(f"tail -n {lines} {log_file_path}")
            log_content = stdout.read().decode('utf-8')
            error = stderr.read().decode('utf-8')
            
            ssh.close()
            
            if error:
                return f"{process_status}\n\nError retrieving log: {error}"
            
            return f"{process_status}\n\nLast {lines} lines of log file:\n\n{log_content}"
            
        except Exception as e:
            return f"Error checking log: {str(e)}"
    
    def _execute_commands(self, instance_id, commands):
        """
        Internal method to execute commands on an EC2 instance via SSH.
        """
        import paramiko
        import os
        
        try:
            # Get instance details
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
            instance = response['Reservations'][0]['Instances'][0]
            public_ip = instance.get('PublicIpAddress')
            
            if not public_ip:
                return {
                    'success': False,
                    'error': "No public IP address found"
                }
            
            if not os.path.exists(self.ssh_key_path):
                return {
                    'success': False,
                    'error': f"SSH key file not found at {self.ssh_key_path}"
                }
            
            # Set up SSH client
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            # Connect to the instance
            print(f"Connecting to {public_ip} as {self.ssh_username}...")
            
            # Retry connection a few times
            max_retries = 5
            retry_delay = 10
            connected = False
            
            for attempt in range(max_retries):
                try:
                    ssh.connect(
                        hostname=public_ip,
                        username=self.ssh_username,
                        key_filename=self.ssh_key_path,
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
                return {
                    'success': False,
                    'error': "Connection failed after multiple attempts"
                }
            
            results = {
                'success': True,
                'commands': []
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
                
                results['commands'].append({
                    'command': cmd,
                    'exit_status': exit_status,
                    'stdout': stdout_content,
                    'stderr': stderr_content
                })
                
                # If a command fails, don't proceed
                if exit_status != 0:
                    print(f"Command failed with exit status {exit_status}")
                    results['success'] = False
                    break
                
                # If this is a nohup command, add context
                if "nohup" in cmd:
                    print("Started background process with nohup. Check log file for progress.")
                    results['commands'][-1]['note'] = "Background process started. Check log file for output."
            
            # Close SSH connection
            ssh.close()
            return results
            
        except Exception as e:
            print(f"Error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }

            
# import boto3
# from create_ec2 import create_ec2_instance
# from setup_ec2 import execute_commands_on_ec2
# from control_ec2 import control_ec2_instance

# import time
# import os
# from dotenv import load_dotenv
# load_dotenv()
# # Replace with your actual values
# AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
# AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
# INSTANCE_ID = os.getenv("INSTANCE_ID")
# SSH_KEY_PATH = os.getenv("SSH_KEY_PATH")
# AWS_REGION = os.getenv("AWS_REGION")
# SSH_USERNAME = os.getenv("SSH_USERNAME")
# LAUNCH_TEMPLATE_ID = os.getenv("LAUNCH_TEMPLATE_ID")

# # Create EC2 instance using the specified launch template
# def create():
#     instance_data = create_ec2_instance(
#         access_key=AWS_ACCESS_KEY,
#         secret_key=AWS_SECRET_KEY,
#         region=AWS_REGION,
#         launch_template_id=LAUNCH_TEMPLATE_ID
#     )
#     time.sleep(60)
#     INSTANCE_ID=instance_data['Instances'][0]['InstanceId']
#     # Set-up the instance
#     install_result = execute_commands_on_ec2(
#         instance_id=INSTANCE_ID,
#         aws_access_key=AWS_ACCESS_KEY,
#         aws_secret_key=AWS_SECRET_KEY,
#         region=AWS_REGION,
#         key_path=SSH_KEY_PATH,
#         username=SSH_USERNAME,
#         commands=["git clone https://github.com/jacksonlmakl/manager.git && cd manager && bash bin/install"]
#     )
    
#     print("\nExecution summary:")
#     print(f"Overall success: {result['success']}")
#     return INSTANCE_ID

# def launch(INSTANCE_ID):
#     return execute_commands_on_ec2(
#         instance_id=INSTANCE_ID,
#         aws_access_key=AWS_ACCESS_KEY,
#         aws_secret_key=AWS_SECRET_KEY,
#         region=AWS_REGION,
#         key_path=SSH_KEY_PATH,
#         username=SSH_USERNAME,
#         commands=["nohup bash ~/manager/launch > ~/launch.log 2>&1 &"]
#     )
# def stop(INSTANCE_ID):
#     return control_ec2_instance(
#         instance_id=INSTANCE_ID,
#         action='stop',  # Use 'stop' to stop the instance
#         aws_access_key=AWS_ACCESS_KEY,
#         aws_secret_key=AWS_SECRET_KEY,
#         region=AWS_REGION,
#         wait_for_completion=True  # Set to False if you don't want to wait
#     )
# def start(INSTANCE_ID):
#     return control_ec2_instance(
#         instance_id=INSTANCE_ID,
#         action='start',  # Use 'stop' to stop the instance
#         aws_access_key=AWS_ACCESS_KEY,
#         aws_secret_key=AWS_SECRET_KEY,
#         region=AWS_REGION,
#         wait_for_completion=True  # Set to False if you don't want to wait
#     )
