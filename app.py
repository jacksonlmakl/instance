import os
import time
import datetime
import pytz
import threading
import traceback
import json
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
    """Function to start an instance on schedule"""
    operations_log.append(f"SCHEDULED: Starting instance {instance_id} based on schedule")
    try:
        manager = EC2Manager(instance_id=instance_id)
        manager.start()
        instances[instance_id]["status"] = "running"
        manager.launch()
        instances[instance_id]["url"] = manager.instance_url
        operations_log.append(f"SCHEDULED: Instance {instance_id} started and launched successfully")
    except Exception as e:
        operations_log.append(f"SCHEDULED ERROR: Failed to start instance {instance_id}: {str(e)}")

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
            manager.start()
            instances[instance_id]["status"] = "starting"
            
            operations_log.append(f"Launching application on {instance_id}...")
            manager.launch()
            instances[instance_id]["status"] = "running"
            
            # Update instance URL
            instances[instance_id]["url"] = manager.instance_url
            operations_log.append(f"Instance {instance_id} is ready at {manager.instance_url}")
            
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
        
        instances[instance_id] = {
            "id": instance_id,
            "status": instance_state,
            "url": manager.instance_url if hasattr(manager, 'instance_url') and manager.instance_url else None
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
    <title>EC2 Manager</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
        :root {
            --primary: #3498db;
            --success: #2ecc71;
            --danger: #e74c3c;
            --warning: #f39c12;
            --dark: #2c3e50;
            --light: #ecf0f1;
        }
        body {
            background-color: #f8f9fa;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            padding-bottom: 50px;
        }
        .navbar {
            background-color: var(--dark);
        }
        .navbar-brand {
            font-weight: bold;
            color: white !important;
        }
        .card {
            border-radius: 10px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            margin-bottom: 20px;
            border: none;
        }
        .card-header {
            background-color: var(--dark);
            color: white;
            border-radius: 10px 10px 0 0 !important;
            font-weight: bold;
        }
        .btn-primary {
            background-color: var(--primary);
            border-color: var(--primary);
        }
        .btn-success {
            background-color: var(--success);
            border-color: var(--success);
        }
        .btn-danger {
            background-color: var(--danger);
            border-color: var(--danger);
        }
        .instance-card {
            transition: all 0.3s ease;
        }
        .instance-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 6px 10px rgba(0, 0, 0, 0.15);
        }
        .status-indicator {
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 5px;
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
            background-color: #9b59b6;
        }
        .log-container {
            background-color: #2c3e50;
            color: #ecf0f1;
            border-radius: 5px;
            padding: 15px;
            font-family: 'Courier New', Courier, monospace;
            max-height: 300px;
            overflow-y: auto;
        }
        .log-entry {
            margin: 0;
            padding: 2px 0;
        }
        .refresh-icon {
            cursor: pointer;
            color: var(--primary);
        }
        .url-link {
            color: var(--primary);
            text-decoration: none;
            font-weight: bold;
        }
        .url-link:hover {
            text-decoration: underline;
        }
        .flash-message {
            border-radius: 5px;
            margin-bottom: 15px;
        }
        .loading-spinner {
            display: inline-block;
            width: 1rem;
            height: 1rem;
            border: 0.2rem solid rgba(255,255,255,.3);
            border-radius: 50%;
            border-top-color: #fff;
            animation: spin 1s linear infinite;
            margin-right: 10px;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        .schedule-info {
            background-color: rgba(52, 152, 219, 0.1);
            border-left: 3px solid var(--primary);
            padding: 5px 8px;
            border-radius: 3px;
            font-size: 0.85rem;
        }
        .schedule-badge {
            background-color: #3498db;
            color: white;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 0.75rem;
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
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark mb-4">
        <div class="container">
            <a class="navbar-brand" href="/">
                <i class="fas fa-cloud me-2"></i> EC2 Manager Dashboard
            </a>
            <div class="d-flex align-items-center">
                <div id="auto-refresh-container" class="text-light me-3">
                    <input type="checkbox" id="auto-refresh" checked>
                    <label for="auto-refresh">Auto refresh</label>
                </div>
                <i class="fas fa-sync-alt refresh-icon text-light" onclick="window.location.reload()"></i>
            </div>
        </div>
    </nav>

    <div class="container">
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                {% for message in messages %}
                    <div class="alert alert-info flash-message">
                        {{ message }}
                    </div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        
        <div class="row">
            <div class="col-md-4">
                <div class="card">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <span><i class="fas fa-plus-circle me-2"></i> New Instance</span>
                    </div>
                    <div class="card-body">
                        <form action="/create" method="post">
                            <button type="submit" class="btn btn-primary w-100">
                                <i class="fas fa-cloud-upload-alt me-2"></i> Create New Instance
                            </button>
                        </form>
                        
                        <hr>
                        
                        <h5>Add Existing Instance</h5>
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
                        <span><i class="fas fa-tasks me-2"></i> Active Tasks</span>
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
                        <span><i class="fas fa-calendar-alt me-2"></i> Scheduled Tasks</span>
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
                        <span><i class="fas fa-server me-2"></i> Managed Instances</span>
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
                                                    {% endif %}
                                                </p>
                                                {% if instance.schedule %}
                                                <div class="schedule-info mb-2">
                                                    <span class="badge bg-info">
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
                            <div class="text-center py-4">
                                <i class="fas fa-cloud-upload-alt fa-3x text-muted mb-3"></i>
                                <p>No instances yet. Create a new one or add an existing instance.</p>
                            </div>
                        {% endif %}
                    </div>
                </div>

                <div class="card">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <span><i class="fas fa-terminal me-2"></i> Operations Log</span>
                        <form action="/clear_logs" method="post" class="m-0">
                            <button type="submit" class="btn btn-sm btn-outline-light">Clear</button>
                        </form>
                    </div>
                    <div class="card-body">
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
                    // Update instance statuses
                    const instancesContainer = document.getElementById('instances-container');
                    // Only refresh the page if data changed significantly
                    if (Object.keys(data.instances).length !== {{ instances|tojson|length }} ||
                        data.tasks.length !== {{ active_tasks|tojson|length }} ||
                        data.log_count > lastLogCount) {
                        
                        window.location.reload();
                        lastLogCount = data.log_count;
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
import atexit

@atexit.register
def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        print("Scheduler shut down successfully")

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
