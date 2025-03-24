import os
import time
import datetime
import pytz
import threading
import traceback
import json
import atexit
from flask import Flask, render_template_string, request, redirect, url_for, flash, jsonify
from controller import EC2Manager
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Store instances in memory - in a real app, you'd use a database
instances = {}
operations_log = []
schedules = {}

# Background task handling
active_tasks = {}

# Initialize scheduler
scheduler = BackgroundScheduler(timezone=pytz.timezone('America/New_York'))
scheduler.start()

def scheduled_start_instance(instance_id):
    """Function to start an instance on schedule with enhanced error handling"""
    operations_log.append(f"SCHEDULED: Starting instance {instance_id} based on schedule")
    
    if instance_id not in instances:
        operations_log.append(f"SCHEDULED ERROR: Instance {instance_id} not found in managed instances")
        return
        
    try:
        # Initialize EC2Manager
        manager = EC2Manager(instance_id=instance_id)
        
        # Check current instance state first
        try:
            response = manager.ec2_client.describe_instances(InstanceIds=[instance_id])
            current_state = response['Reservations'][0]['Instances'][0]['State']['Name']
            operations_log.append(f"SCHEDULED: Current state of instance {instance_id} is '{current_state}'")
            
            # Update our local status
            instances[instance_id]["status"] = current_state
        except Exception as e:
            operations_log.append(f"SCHEDULED ERROR: Failed to check state: {str(e)}")
            current_state = "unknown"
        
        # If instance is not running, start it explicitly with AWS API call
        if current_state != "running":
            operations_log.append(f"SCHEDULED: Instance is not running. Explicitly starting {instance_id}...")
            
            try:
                # Direct EC2 API call to start the instance
                start_response = manager.ec2_client.start_instances(InstanceIds=[instance_id])
                operations_log.append(f"SCHEDULED: EC2 start API response: {start_response}")
                
                # Update instance status to starting
                instances[instance_id]["status"] = "starting"
                
                # Wait for the instance to be in running state (up to 2 minutes)
                operations_log.append(f"SCHEDULED: Waiting for instance {instance_id} to reach 'running' state...")
                waiter = manager.ec2_client.get_waiter('instance_running')
                waiter.wait(
                    InstanceIds=[instance_id],
                    WaiterConfig={
                        'Delay': 5,       # Check every 5 seconds
                        'MaxAttempts': 24 # Up to 2 minutes (24 * 5s)
                    }
                )
                operations_log.append(f"SCHEDULED: Instance {instance_id} is now in 'running' state")
                
                # Update our local status
                instances[instance_id]["status"] = "running"
            except Exception as e:
                operations_log.append(f"SCHEDULED ERROR: Failed during instance start: {str(e)}")
                return
        
        # Now that we know the instance is running, let's wait a bit to ensure services are ready
        operations_log.append(f"SCHEDULED: Waiting for services to initialize on {instance_id}...")
        time.sleep(10)  # Give some time for the instance to fully initialize
        
        # Now launch the application
        operations_log.append(f"SCHEDULED: Launching application on instance {instance_id}...")
        try:
            # Use the launch method from EC2Manager
            launch_result = manager.launch()
            operations_log.append(f"SCHEDULED: Launch result: {launch_result}")
            
            # Update instance URL
            try:
                # Get the latest instance information
                response = manager.ec2_client.describe_instances(InstanceIds=[instance_id])
                instance = response['Reservations'][0]['Instances'][0]
                public_ip = instance.get('PublicIpAddress')
                
                if public_ip:
                    instances[instance_id]["url"] = f"http://{public_ip}:1100"
                    operations_log.append(f"SCHEDULED: Instance {instance_id} is ready at {instances[instance_id]['url']}")
                else:
                    operations_log.append(f"SCHEDULED WARNING: No public IP found for instance {instance_id}")
            except Exception as e:
                operations_log.append(f"SCHEDULED ERROR: Failed to get URL: {str(e)}")
        except Exception as e:
            operations_log.append(f"SCHEDULED ERROR: Failed to launch application: {str(e)}")
            
    except Exception as e:
        operations_log.append(f"SCHEDULED ERROR: Unhandled exception in scheduled start: {str(e)}")
        operations_log.append(traceback.format_exc())


def scheduled_stop_instance(instance_id):
    """Function to stop an instance on schedule"""
    operations_log.append(f"SCHEDULED: Stopping instance {instance_id} based on schedule")
    try:
        manager = EC2Manager(instance_id=instance_id)
        manager.stop()
        instances[instance_id]["status"] = "stopped"
        instances[instance_id]["url"] = None
        operations_log.append(f"SCHEDULED: Instance {instance_id} stopped successfully")
    except Exception as e:
        operations_log.append(f"SCHEDULED ERROR: Failed to stop instance {instance_id}: {str(e)}")

def add_daily_schedule(instance_id, start_time, duration_minutes):
    """Add a daily schedule for an instance"""
    global schedules
    
    # Parse the start time (format: HH:MM)
    start_hour, start_minute = map(int, start_time.split(':'))
    
    # Calculate end time based on duration
    end_hour = start_hour + (duration_minutes // 60)
    end_minute = start_minute + (duration_minutes % 60)
    
    # Handle minute overflow
    if end_minute >= 60:
        end_hour += 1
        end_minute -= 60
    
    # Handle hour overflow
    end_hour = end_hour % 24
    
    # Format times for display
    end_time = f"{end_hour:02d}:{end_minute:02d}"
    
    # Add schedule to our schedules dict
    if instance_id not in schedules:
        schedules[instance_id] = {"start": start_time, "end": end_time, "duration": duration_minutes}
    else:
        # Update existing schedule
        schedules[instance_id]["start"] = start_time
        schedules[instance_id]["end"] = end_time
        schedules[instance_id]["duration"] = duration_minutes
    
    # Remove any existing jobs for this instance
    for job in scheduler.get_jobs():
        if job.id.startswith(f"start_{instance_id}_") or job.id.startswith(f"stop_{instance_id}_"):
            scheduler.remove_job(job.id)
    
    # Add start job
    start_job_id = f"start_{instance_id}_{int(time.time())}"
    scheduler.add_job(
        scheduled_start_instance,
        CronTrigger(hour=start_hour, minute=start_minute),
        args=[instance_id],
        id=start_job_id,
        replace_existing=True
    )
    
    # Add stop job
    stop_job_id = f"stop_{instance_id}_{int(time.time())}"
    scheduler.add_job(
        scheduled_stop_instance,
        CronTrigger(hour=end_hour, minute=end_minute),
        args=[instance_id],
        id=stop_job_id,
        replace_existing=True
    )
    
    operations_log.append(f"Schedule added for instance {instance_id}: ON at {start_time}, OFF at {end_time} (duration: {duration_minutes} minutes)")
    
    # Add schedule info to instance data
    if instance_id in instances:
        instances[instance_id]["schedule"] = {
            "start": start_time,
            "end": end_time,
            "duration": duration_minutes
        }

def remove_schedule(instance_id):
    """Remove schedule for an instance"""
    global schedules
    
    # Remove any existing jobs for this instance
    for job in scheduler.get_jobs():
        if job.id.startswith(f"start_{instance_id}_") or job.id.startswith(f"stop_{instance_id}_"):
            scheduler.remove_job(job.id)
    
    # Remove from schedules dict
    if instance_id in schedules:
        del schedules[instance_id]
    
    # Remove schedule info from instance data
    if instance_id in instances and "schedule" in instances[instance_id]:
        del instances[instance_id]["schedule"]
    
    operations_log.append(f"Schedule removed for instance {instance_id}")

def background_task(task_id, operation, instance_id=None):
    global active_tasks, instances, operations_log
    
    try:
        if operation == "create":
            manager = EC2Manager()
            operations_log.append(f"Creating new instance...")
            new_instance_id = manager.create()
            instances[new_instance_id] = {
                "id": new_instance_id,
                "status": "created",
                "url": None
            }
            
            operations_log.append(f"Setting up instance {new_instance_id}...")
            manager.setup()
            instances[new_instance_id]["status"] = "setup"
            
            operations_log.append(f"Launching application on {new_instance_id}...")
            manager.launch()
            instances[new_instance_id]["status"] = "running"
            
            # Update instance URL
            instances[new_instance_id]["url"] = manager.instance_url
            operations_log.append(f"Instance {new_instance_id} is ready at {manager.instance_url}")
            
        elif operation == "start":
            manager = EC2Manager(instance_id=instance_id)
            operations_log.append(f"Starting instance {instance_id}...")
            start_result = manager.start()
            instances[instance_id]["status"] = "starting"
            
            if start_result.get('success', False):
                operations_log.append(f"Launching application on {instance_id}...")
                launch_result = manager.launch()
                instances[instance_id]["status"] = "running"
                
                # Explicitly check for the instance URL after launching
                try:
                    # Get the latest instance information and update the URL
                    response = manager.ec2_client.describe_instances(InstanceIds=[instance_id])
                    instance = response['Reservations'][0]['Instances'][0]
                    public_ip = instance.get('PublicIpAddress')
                    
                    # Use the current URL generation logic that's in your EC2Manager
                    if public_ip:
                        instances[instance_id]["url"] = f"http://{public_ip}:1100"
                        operations_log.append(f"Instance {instance_id} is ready at {instances[instance_id]['url']}")
                    else:
                        operations_log.append(f"Warning: No public IP found for instance {instance_id}")
                except Exception as e:
                    operations_log.append(f"Error getting instance URL: {str(e)}")
            else:
                operations_log.append(f"Failed to start instance {instance_id}")
            
        elif operation == "stop":
            manager = EC2Manager(instance_id=instance_id)
            operations_log.append(f"Stopping instance {instance_id}...")
            manager.stop()
            instances[instance_id]["status"] = "stopped"
            instances[instance_id]["url"] = None
            operations_log.append(f"Instance {instance_id} stopped successfully")
    
    except Exception as e:
        error_msg = f"Error in {operation} operation: {str(e)}"
        operations_log.append(error_msg)
        operations_log.append(traceback.format_exc())
        if instance_id and instance_id in instances:
            instances[instance_id]["status"] = "error"
    
    finally:
        # Remove task from active tasks
        if task_id in active_tasks:
            del active_tasks[task_id]

@app.route('/')
def index():
    # Get all active schedules
    active_schedules = {}
    for job in scheduler.get_jobs():
        if job.id.startswith("start_") or job.id.startswith("stop_"):
            job_parts = job.id.split('_')
            if len(job_parts) > 1:
                instance_id = job_parts[1]
                if instance_id not in active_schedules:
                    active_schedules[instance_id] = []
                active_schedules[instance_id].append({
                    "job_id": job.id,
                    "next_run": job.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if job.next_run_time else "N/A",
                    "type": job_parts[0]
                })
    
    return render_template_string(HTML_TEMPLATE, 
                                  instances=instances, 
                                  operations_log=operations_log,
                                  active_tasks=active_tasks,
                                  schedules=schedules,
                                  active_schedules=active_schedules)

@app.route('/create', methods=['POST'])
def create_instance():
    task_id = f"create_{int(time.time())}"
    active_tasks[task_id] = "Creating new instance"
    
    thread = threading.Thread(
        target=background_task,
        args=(task_id, "create")
    )
    thread.daemon = True
    thread.start()
    
    flash("Creating new instance. This may take a few minutes.")
    return redirect(url_for('index'))

@app.route('/start/<instance_id>', methods=['POST'])
def start_instance(instance_id):
    if instance_id not in instances:
        flash(f"Instance {instance_id} not found")
        return redirect(url_for('index'))
    
    task_id = f"start_{instance_id}_{int(time.time())}"
    active_tasks[task_id] = f"Starting instance {instance_id}"
    
    thread = threading.Thread(
        target=background_task,
        args=(task_id, "start", instance_id)
    )
    thread.daemon = True
    thread.start()
    
    flash(f"Starting instance {instance_id}. This may take a few minutes.")
    return redirect(url_for('index'))

@app.route('/stop/<instance_id>', methods=['POST'])
def stop_instance(instance_id):
    if instance_id not in instances:
        flash(f"Instance {instance_id} not found")
        return redirect(url_for('index'))
    
    task_id = f"stop_{instance_id}_{int(time.time())}"
    active_tasks[task_id] = f"Stopping instance {instance_id}"
    
    thread = threading.Thread(
        target=background_task,
        args=(task_id, "stop", instance_id)
    )
    thread.daemon = True
    thread.start()
    
    flash(f"Stopping instance {instance_id}. This may take a few minutes.")
    return redirect(url_for('index'))

@app.route('/add_existing', methods=['POST'])
def add_existing_instance():
    instance_id = request.form.get('instance_id')
    if not instance_id:
        flash("Please provide an instance ID")
        return redirect(url_for('index'))
    
    try:
        manager = EC2Manager(instance_id=instance_id)
        # Try to get instance info to validate it exists
        response = manager.ec2_client.describe_instances(InstanceIds=[instance_id])
        instance_state = response['Reservations'][0]['Instances'][0]['State']['Name']
        public_ip = response['Reservations'][0]['Instances'][0].get('PublicIpAddress')
        
        # Create URL if instance is running and has a public IP
        instance_url = None
        if instance_state == 'running' and public_ip:
            instance_url = f"http://{public_ip}:1100"
        
        instances[instance_id] = {
            "id": instance_id,
            "status": instance_state,
            "url": instance_url
        }
        
        # Check if this instance has a schedule
        if instance_id in schedules:
            instances[instance_id]["schedule"] = schedules[instance_id]
        
        flash(f"Instance {instance_id} added successfully")
        operations_log.append(f"Added existing instance {instance_id}")
    except Exception as e:
        flash(f"Error adding instance: {str(e)}")
        operations_log.append(f"Error adding instance {instance_id}: {str(e)}")
    
    return redirect(url_for('index'))

@app.route('/schedule/<instance_id>', methods=['POST'])
def set_schedule(instance_id):
    if instance_id not in instances:
        flash(f"Instance {instance_id} not found")
        return redirect(url_for('index'))
    
    start_time = request.form.get('start_time')
    duration = request.form.get('duration')
    
    if not start_time or not duration:
        flash("Please provide both start time and duration")
        return redirect(url_for('index'))
    
    try:
        duration_minutes = int(duration)
        if duration_minutes <= 0:
            raise ValueError("Duration must be positive")
        
        # Validate start_time format (HH:MM)
        datetime.datetime.strptime(start_time, '%H:%M')
        
        add_daily_schedule(instance_id, start_time, duration_minutes)
        flash(f"Schedule added for instance {instance_id}")
    except ValueError as e:
        flash(f"Invalid schedule parameters: {str(e)}")
    
    return redirect(url_for('index'))

@app.route('/refresh_url/<instance_id>', methods=['POST'])
def refresh_url(instance_id):
    """Explicitly refresh the URL of an instance"""
    if instance_id not in instances:
        flash(f"Instance {instance_id} not found")
        return redirect(url_for('index'))
    
    try:
        manager = EC2Manager(instance_id=instance_id)
        
        # Get the latest instance information
        response = manager.ec2_client.describe_instances(InstanceIds=[instance_id])
        instance_state = response['Reservations'][0]['Instances'][0]['State']['Name']
        public_ip = response['Reservations'][0]['Instances'][0].get('PublicIpAddress')
        
        # Update instance status
        instances[instance_id]["status"] = instance_state
        
        # Update URL if instance is running
        if instance_state == 'running' and public_ip:
            instances[instance_id]["url"] = f"http://{public_ip}:1100"
            flash(f"URL updated for instance {instance_id}")
            operations_log.append(f"URL refreshed for instance {instance_id}: {instances[instance_id]['url']}")
        else:
            instances[instance_id]["url"] = None
            flash(f"Instance {instance_id} is not running or has no public IP")
    except Exception as e:
        flash(f"Error refreshing URL: {str(e)}")
        operations_log.append(f"Error refreshing URL for instance {instance_id}: {str(e)}")
    
    return redirect(url_for('index'))

@app.route('/remove_schedule/<instance_id>', methods=['POST'])
def remove_instance_schedule(instance_id):
    if instance_id not in instances:
        flash(f"Instance {instance_id} not found")
        return redirect(url_for('index'))
    
    remove_schedule(instance_id)
    flash(f"Schedule removed for instance {instance_id}")
    return redirect(url_for('index'))

@app.route('/status')
def status():
    return jsonify({
        "instances": instances,
        "tasks": list(active_tasks.keys()),
        "log_count": len(operations_log),
        "schedules": schedules
    })

@app.route('/remove_instance/<instance_id>', methods=['POST'])
def remove_instance(instance_id):
    if instance_id in instances:
        # Remove any schedules for this instance
        remove_schedule(instance_id)
        
        # Remove instance from dashboard
        del instances[instance_id]
        flash(f"Instance {instance_id} removed from dashboard")
        operations_log.append(f"Removed instance {instance_id} from dashboard")
    else:
        flash(f"Instance {instance_id} not found")
    
    return redirect(url_for('index'))

@app.route('/clear_logs', methods=['POST'])
def clear_logs():
    global operations_log
    operations_log = []
    flash("Operation logs cleared")
    return redirect(url_for('index'))

# HTML Template with modern styling
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Instance Manager</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
        :root {
            --primary: #4F46E5;
            --primary-dark: #4338CA;
            --primary-light: #EEF2FF;
            --secondary: #64748B;
            --success: #10B981;
            --info: #0EA5E9;
            --warning: #F59E0B;
            --danger: #EF4444;
            --light: #F1F5F9;
            --dark: #1E293B;
            --surface: #FFFFFF;
            --bg: #F8FAFC;
            --border: #E2E8F0;
            --text: #0F172A;
            --text-secondary: #64748B;
            --shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
            --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
            --transition: all 0.2s ease-in-out;
        }
        
        body {
            background-color: var(--bg);
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif;
            padding-bottom: 50px;
            color: var(--text);
        }
        
        .navbar {
            background: linear-gradient(135deg, var(--primary), var(--primary-dark));
            box-shadow: var(--shadow);
        }
        
        .navbar-brand {
            font-weight: bold;
            color: white !important;
            letter-spacing: -0.5px;
        }
        
        .card {
            border-radius: 16px;
            box-shadow: var(--shadow);
            margin-bottom: 24px;
            border: 1px solid var(--border);
            transition: var(--transition);
            background-color: var(--surface);
        }
        
        .card:hover {
            box-shadow: var(--shadow-lg);
        }
        
        .card-header {
            background: linear-gradient(135deg, var(--dark), #334155);
            color: white;
            border-radius: 16px 16px 0 0 !important;
            font-weight: 600;
            padding: 16px 20px;
            border-bottom: none;
        }
        
        .card-header i {
            margin-right: 8px;
            color: var(--primary-light);
        }
        
        .card-body {
            padding: 20px;
        }
        
        .btn {
            border-radius: 10px;
            font-weight: 500;
            transition: var(--transition);
            padding: 8px 16px;
        }
        
        .btn-primary {
            background: linear-gradient(135deg, var(--primary), var(--primary-dark));
            border-color: var(--primary-dark);
        }
        
        .btn-primary:hover {
            background: linear-gradient(135deg, #4338CA, #3730A3);
            border-color: #3730A3;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(79, 70, 229, 0.2);
        }
        
        .btn-success {
            background: linear-gradient(135deg, var(--success), #059669);
            border-color: #059669;
        }
        
        .btn-success:hover {
            background: linear-gradient(135deg, #059669, #047857);
            border-color: #047857;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(16, 185, 129, 0.2);
        }
        
        .btn-danger {
            background: linear-gradient(135deg, var(--danger), #DC2626);
            border-color: #DC2626;
        }
        
        .btn-danger:hover {
            background: linear-gradient(135deg, #DC2626, #B91C1C);
            border-color: #B91C1C;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(239, 68, 68, 0.2);
        }
        
        .btn-outline-primary {
            color: var(--primary);
            border-color: var(--primary);
        }
        
        .btn-outline-primary:hover {
            background-color: var(--primary);
            color: white;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(79, 70, 229, 0.1);
        }
        
        .btn-outline-secondary {
            color: var(--secondary);
            border-color: var(--secondary);
        }
        
        .btn-outline-secondary:hover {
            background-color: var(--secondary);
            color: white;
            transform: translateY(-1px);
        }
        
        .btn-outline-info {
            color: var(--info);
            border-color: var(--info);
        }
        
        .btn-outline-info:hover {
            background-color: var(--info);
            color: white;
            transform: translateY(-1px);
        }
        
        .btn-outline-light {
            color: white;
            border-color: rgba(255, 255, 255, 0.5);
        }
        
        .btn-outline-light:hover {
            background-color: rgba(255, 255, 255, 0.1);
            color: white;
            border-color: white;
        }
        
        .instance-card {
            transition: var(--transition);
            border: 1px solid var(--border);
        }
        
        .instance-card:hover {
            transform: translateY(-5px);
            box-shadow: var(--shadow-lg);
        }
        
        .instance-card .card-body {
            padding: 16px;
        }
        
        .status-indicator {
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 8px;
            box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.8);
        }
        
        .status-running, .status-available {
            background-color: var(--success);
        }
        
        .status-stopped, .status-stopping {
            background-color: var(--danger);
        }
        
        .status-starting, .status-pending, .status-created, .status-setup {
            background-color: var(--warning);
        }
        
        .status-error {
            background-color: #9333EA;
        }
        
        .log-container {
            background-color: #0F172A;
            color: #E2E8F0;
            border-radius: 12px;
            padding: 16px;
            font-family: 'Cascadia Code', 'Fira Code', 'JetBrains Mono', 'Courier New', monospace;
            max-height: 300px;
            overflow-y: auto;
            box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.2);
        }
        
        .log-entry {
            margin: 0;
            padding: 3px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
        }
        
        .log-entry:last-child {
            border-bottom: none;
        }
        
        .refresh-icon {
            cursor: pointer;
            color: white;
            transition: var(--transition);
        }
        
        .refresh-icon:hover {
            transform: rotate(180deg);
        }
        
        .url-link {
            color: var(--primary);
            text-decoration: none;
            font-weight: 500;
            transition: var(--transition);
        }
        
        .url-link:hover {
            color: var(--primary-dark);
            text-decoration: underline;
        }
        
        .flash-message {
            border-radius: 12px;
            margin-bottom: 20px;
            background-color: #EFF6FF;
            border-left: 4px solid var(--primary);
            color: var(--primary-dark);
            padding: 12px 16px;
            box-shadow: var(--shadow);
        }
        
        .loading-spinner {
            display: inline-block;
            width: 1rem;
            height: 1rem;
            border: 0.2rem solid rgba(79, 70, 229, 0.2);
            border-radius: 50%;
            border-top-color: var(--primary);
            animation: spin 1s linear infinite;
            margin-right: 10px;
        }
        
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        .schedule-info {
            background-color: var(--primary-light);
            border-left: 3px solid var(--primary);
            padding: 8px 12px;
            border-radius: 8px;
            font-size: 0.85rem;
            margin-bottom: 12px;
        }
        
        .schedule-badge {
            background: linear-gradient(135deg, var(--info), #0284C7);
            color: white;
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 500;
            display: inline-flex;
            align-items: center;
        }
        
        .time-picker {
            max-width: 150px;
        }
        
        .toast-container {
            position: fixed;
            bottom: 20px;
            right: 20px;
            z-index: 1050;
        }
        
        /* Form Controls */
        .form-control {
            border-radius: 10px;
            padding: 10px 14px;
            border: 1px solid var(--border);
            transition: var(--transition);
        }
        
        .form-control:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.15);
        }
        
        .form-control-sm {
            border-radius: 8px;
            padding: 8px 12px;
        }
        
        .form-label {
            font-weight: 500;
            color: var(--text);
            margin-bottom: 6px;
        }
        
        /* Badge styling */
        .badge {
            font-weight: 500;
            padding: 4px 10px;
            border-radius: 8px;
        }
        
        .bg-primary {
            background: linear-gradient(135deg, var(--primary), var(--primary-dark)) !important;
        }
        
        .bg-success {
            background: linear-gradient(135deg, var(--success), #059669) !important;
        }
        
        .bg-danger {
            background: linear-gradient(135deg, var(--danger), #DC2626) !important;
        }
        
        .bg-secondary {
            background: linear-gradient(135deg, var(--secondary), #475569) !important;
        }
        
        .bg-info {
            background: linear-gradient(135deg, var(--info), #0284C7) !important;
        }
        
        .bg-warning {
            background: linear-gradient(135deg, var(--warning), #D97706) !important;
        }
        
        /* List groups */
        .list-group-item {
            border-radius: 10px;
            margin-bottom: 8px;
            border: 1px solid var(--border);
            padding: 12px 16px;
        }
        
        .list-group-item:last-child {
            margin-bottom: 0;
        }
        
        /* Auto refresh toggle */
        .form-check-input {
            width: 18px;
            height: 18px;
            margin-top: 0.2em;
            cursor: pointer;
            border: 2px solid rgba(255, 255, 255, 0.5);
        }
        
        .form-check-input:checked {
            background-color: white;
            border-color: white;
        }
        
        .form-check-input:checked[type=checkbox] {
            background-image: url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20'%3e%3cpath fill='%234F46E5' d='M14.293 5.293a1 1 0 0 0-1.414 0L7 11.586 4.707 9.293a1 1 0 0 0-1.414 1.414l3 3a1 1 0 0 0 1.414 0l7-7a1 1 0 0 0 0-1.414z'/%3e%3c/svg%3e");
        }
        
        .form-check-label {
            color: white;
            cursor: pointer;
        }
        
        /* Collapse areas */
        .collapse {
            transition: var(--transition);
        }
        
        /* Empty state */
        .empty-state {
            text-align: center;
            padding: 32px 16px;
        }
        
        .empty-state i {
            font-size: 3rem;
            margin-bottom: 16px;
            color: var(--text-secondary);
            background: linear-gradient(135deg, var(--primary-light), #E2E8F0);
            width: 80px;
            height: 80px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0 auto 20px auto;
        }
        
        /* Status section */
        .status-section {
            display: flex;
            align-items: center;
            margin-bottom: 12px;
        }
        
        .terminal-header {
            background-color: #1A1A1A;
            color: #E0E0E0;
            padding: 10px 16px;
            border-radius: 12px 12px 0 0;
            font-family: 'Cascadia Code', 'Fira Code', 'JetBrains Mono', 'Courier New', monospace;
            font-size: 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .terminal-header .dots {
            display: flex;
            gap: 6px;
        }
        
        .terminal-header .dot {
            width: 12px;
            height: 12px;
            border-radius: 50%;
        }
        
        .terminal-header .dot-red { background-color: #FF5F56; }
        .terminal-header .dot-yellow { background-color: #FFBD2E; }
        .terminal-header .dot-green { background-color: #27C93F; }
        
        .log-container {
            border-radius: 0 0 12px 12px;
        }
        
        .btn-group > form {
            margin-bottom: 0;
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark mb-4">
        <div class="container">
            <a class="navbar-brand" href="/">
                <i class="fas fa-cloud me-2"></i> Instance Dashboard
            </a>
            <div class="d-flex align-items-center">
                <div id="auto-refresh-container" class="form-check form-switch me-3">
                    <input type="checkbox" class="form-check-input" id="auto-refresh" checked>
                    <label class="form-check-label" for="auto-refresh">Auto refresh</label>
                </div>
                <i class="fas fa-sync-alt refresh-icon" onclick="window.location.reload()"></i>
            </div>
        </div>
    </nav>

    <div class="container">
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                {% for message in messages %}
                    <div class="alert flash-message">
                        <i class="fas fa-info-circle me-2"></i>
                        {{ message }}
                    </div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        
        <div class="row">
            <div class="col-md-4">
                <div class="card">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <span><i class="fas fa-plus-circle"></i> New Instance</span>
                    </div>
                    <div class="card-body">
                        <form action="/create" method="post">
                            <button type="submit" class="btn btn-primary w-100">
                                <i class="fas fa-cloud-upload-alt me-2"></i> Create New Instance
                            </button>
                        </form>
                        
                        <hr>
                        
                        <h5 class="mb-3">Add Existing Instance</h5>
                        <form action="/add_existing" method="post">
                            <div class="mb-3">
                                <input type="text" class="form-control" name="instance_id" placeholder="i-0123456789abcdef" required>
                            </div>
                            <button type="submit" class="btn btn-outline-primary w-100">
                                <i class="fas fa-link me-2"></i> Add Instance
                            </button>
                        </form>
                    </div>
                </div>

                <div class="card">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <span><i class="fas fa-tasks"></i> Active Tasks</span>
                    </div>
                    <div class="card-body">
                        <div id="active-tasks-container">
                            {% if active_tasks %}
                                <ul class="list-group">
                                {% for task_id, task_desc in active_tasks.items() %}
                                    <li class="list-group-item d-flex justify-content-between align-items-center">
                                        <div>
                                            <div class="loading-spinner"></div>
                                            {{ task_desc }}
                                        </div>
                                    </li>
                                {% endfor %}
                                </ul>
                            {% else %}
                                <p class="text-muted">No active tasks</p>
                            {% endif %}
                        </div>
                    </div>
                </div>
                
                <div class="card">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <span><i class="fas fa-calendar-alt"></i> Scheduled Tasks</span>
                    </div>
                    <div class="card-body">
                        <div id="schedules-container">
                            {% if schedules %}
                                <ul class="list-group">
                                {% for instance_id, schedule in schedules.items() %}
                                    <li class="list-group-item d-flex justify-content-between align-items-center">
                                        <div>
                                            <i class="fas fa-server me-2 text-primary"></i>
                                            {{ instance_id[:8] }}...
                                            <br>
                                            <small class="text-muted">
                                                ON: <span class="text-success">{{ schedule.start }}</span> | 
                                                OFF: <span class="text-danger">{{ schedule.end }}</span>
                                                ({{ schedule.duration }} mins)
                                            </small>
                                        </div>
                                        <form action="/remove_schedule/{{ instance_id }}" method="post">
                                            <button type="submit" class="btn btn-sm btn-outline-danger">
                                                <i class="fas fa-trash"></i>
                                            </button>
                                        </form>
                                    </li>
                                {% endfor %}
                                </ul>
                            {% else %}
                                <p class="text-muted">No scheduled tasks</p>
                            {% endif %}
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="col-md-8">
                <div class="card">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <span><i class="fas fa-server"></i> Managed Instances</span>
                        <span class="badge bg-primary">{{ instances|length }}</span>
                    </div>
                    <div class="card-body">
                        {% if instances %}
                            <div class="row" id="instances-container">
                                {% for instance_id, instance in instances.items() %}
                                    <div class="col-md-6 mb-3">
                                        <div class="card instance-card">
                                            <div class="card-body">
                                                <h5 class="card-title d-flex justify-content-between">
                                                    <span>
                                                        <span class="status-indicator status-{{ instance.status }}"></span>
                                                        {{ instance_id[:8] }}...
                                                    </span>
                                                    <span class="badge bg-secondary">{{ instance.status }}</span>
                                                </h5>
                                                <p class="card-text">
                                                    {% if instance.url %}
                                                        <strong>URL:</strong> <a href="{{ instance.url }}" target="_blank" class="url-link">{{ instance.url }}</a>
                                                    {% else %}
                                                        <span class="text-muted">No URL available</span>
                                                        {% if instance.status == 'running' %}
                                                        <form action="/refresh_url/{{ instance_id }}" method="post" class="d-inline">
                                                            <button type="submit" class="btn btn-sm btn-link text-primary p-0 ms-2">
                                                                <i class="fas fa-sync-alt"></i> Refresh
                                                            </button>
                                                        </form>
                                                        {% endif %}
                                                    {% endif %}
                                                </p>
                                                {% if instance.schedule %}
                                                <div class="schedule-info mb-2">
                                                    <span class="schedule-badge">
                                                        <i class="fas fa-clock me-1"></i> 
                                                        On: {{ instance.schedule.start }} | Off: {{ instance.schedule.end }}
                                                    </span>
                                                    <form action="/remove_schedule/{{ instance_id }}" method="post" class="d-inline">
                                                        <button type="submit" class="btn btn-sm btn-link text-danger p-0 ms-2">
                                                            <i class="fas fa-times-circle"></i>
                                                        </button>
                                                    </form>
                                                </div>
                                                {% endif %}
                                                <div class="btn-group w-100">
                                                    <form action="/start/{{ instance_id }}" method="post" class="me-2">
                                                        <button class="btn btn-sm btn-success" {% if instance.status == 'running' %}disabled{% endif %}>
                                                            <i class="fas fa-play me-1"></i> Start
                                                        </button>
                                                    </form>
                                                    <form action="/stop/{{ instance_id }}" method="post" class="me-2">
                                                        <button class="btn btn-sm btn-danger" {% if instance.status == 'stopped' %}disabled{% endif %}>
                                                            <i class="fas fa-stop me-1"></i> Stop
                                                        </button>
                                                    </form>
                                                    <form action="/remove_instance/{{ instance_id }}" method="post">
                                                        <button class="btn btn-sm btn-outline-secondary">
                                                            <i class="fas fa-trash me-1"></i>
                                                        </button>
                                                    </form>
                                                </div>
                                                
                                                {% if not instance.schedule %}
                                                <div class="mt-2">
                                                    <button class="btn btn-sm btn-outline-info w-100" data-bs-toggle="collapse" data-bs-target="#schedule-form-{{ instance_id }}">
                                                        <i class="fas fa-clock me-1"></i> Set Schedule
                                                    </button>
                                                    <div class="collapse mt-2" id="schedule-form-{{ instance_id }}">
                                                        <form action="/schedule/{{ instance_id }}" method="post">
                                                            <div class="mb-2">
                                                                <label class="form-label small">Start Time (24h format)</label>
                                                                <input type="time" class="form-control form-control-sm" name="start_time" required>
                                                            </div>
                                                            <div class="mb-2">
                                                                <label class="form-label small">Duration (minutes)</label>
                                                                <input type="number" class="form-control form-control-sm" name="duration" min="15" step="15" value="90" required>
                                                            </div>
                                                            <button type="submit" class="btn btn-sm btn-primary w-100">Save Schedule</button>
                                                        </form>
                                                    </div>
                                                </div>
                                                {% endif %}
                                            </div>
                                        </div>
                                    </div>
                                {% endfor %}
                            </div>
                        {% else %}
                            <div class="empty-state">
                                <i class="fas fa-cloud-upload-alt"></i>
                                <p>No instances yet. Create a new one or add an existing instance.</p>
                            </div>
                        {% endif %}
                    </div>
                </div>

                <div class="card">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <span><i class="fas fa-terminal"></i> Operations Log</span>
                        <form action="/clear_logs" method="post" class="m-0">
                            <button type="submit" class="btn btn-sm btn-outline-light">Clear</button>
                        </form>
                    </div>
                    <div class="card-body p-0">
                        <div class="terminal-header">
                            <span>ec2manager@console</span>
                            <div class="dots">
                                <div class="dot dot-red"></div>
                                <div class="dot dot-yellow"></div>
                                <div class="dot dot-green"></div>
                            </div>
                        </div>
                        <div class="log-container" id="log-container">
                            {% if operations_log %}
                                {% for log in operations_log %}
                                    <p class="log-entry">$ {{ log }}</p>
                                {% endfor %}
                            {% else %}
                                <p class="log-entry text-muted">No operations logged yet.</p>
                            {% endif %}
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div class="toast-container"></div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        // Auto-refresh functionality
        let autoRefreshEnabled = true;
        const autoRefreshCheckbox = document.getElementById('auto-refresh');
        let lastLogCount = {{ operations_log|length }};
        
        autoRefreshCheckbox.addEventListener('change', function() {
            autoRefreshEnabled = this.checked;
        });
        
        function refreshData() {
            if (!autoRefreshEnabled) return;
            
            fetch('/status')
                .then(response => response.json())
                .then(data => {
                    // Check if data changed significantly
                    const instanceCount = Object.keys(data.instances).length;
                    const taskCount = data.tasks.length;
                    const logCount = data.log_count;
                    
                    // Only refresh if needed
                    if (instanceCount !== {{ instances|length }} || 
                        taskCount !== {{ active_tasks|length }} || 
                        logCount > lastLogCount) {
                        window.location.reload();
                        lastLogCount = logCount;
                    }
                })
                .catch(error => console.error('Error refreshing data:', error));
        }
        
        // Refresh every 5 seconds
        setInterval(refreshData, 5000);
        
        // Scroll to the bottom of the log container initially
        const logContainer = document.getElementById('log-container');
        if (logContainer) {
            logContainer.scrollTop = logContainer.scrollHeight;
        }
    </script>
</body>
</html>
"""
# Handle graceful shutdown
@atexit.register
def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        print("Scheduler shut down successfully")

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
