
# ShotGrid-Resilio Webhooks Integration

Automated system that keeps Resilio Active Everywhere hybrid work jobs connected to ShotGrid shot statuses and task assignments.

## Overview

This Firebase Cloud Function service monitors ShotGrid webhooks and automatically manages Resilio Connect jobs to ensure artists have access to the right files at the right time. When shots become active or artists are assigned/removed, the system creates, updates, or deletes Resilio HybridWork jobs accordingly.

![shotsmappedtojobs](https://bucket.camerontarget.com/resilio_run_through/workflow.gif)

## Features

### **Automatic Job Management**
- **Creates** Resilio jobs when shots become active and artists are assigned
- **Updates** jobs when artist assignments change
- **Deletes** jobs when shots become inactive or all artists are removed
- Sets up **One job per shot** with all assigned artists as end user agents

### **Multi-Artist & Multi-Task Support**
- Handles multiple artists on a single task (e.g Comp assigned to Alex + Matthew)
- Aggregates artists across multiple tasks per shot (e.g Alex on Comp, Matthew on Paint)
- Adds/removes artists/agents from jobs as assignments change

### **Project Assets Folder**
- Creates shared asset jobs for each project (this can be expanded depending on needs, it's a simpler placeholder for a more robust assets workflow)
- Includes all artists working on any shot in that project
- Automatically manages access as artists join/leave projects

### **Simpler Configuration Set-up**
- YAML-based artist and path configuration
- Cross-platform file paths etc...
- Configurable primary storage and target agent mappings (should align with SGTK templates and or schema)

## Required ShotGrid Webhook Setup

Needs these webhooks on your ShotGrid site:

### 1. Shot Status Change Webhook
- **Event Type**: `Shotgun_Shot_Change`
- **Conditions**: `sg_status_list` field changes
- **URL**: `https://your-firebase-url/shot_status_webhook`
- **Triggers**: Full sync when shots become active/inactive (uses a status with shortcode currently set to `active`)

### 2. Task Assignment Change Webhook
- **Event Type**: `Shotgun_Task_Change`
- **Conditions**: `task_assignees` field changes
- **URL**: `https://your-firebase-url/assignment_webhook`
- **Triggers**: Changes Resilio job configurations when artists are added/removed from tasks

## Configuration

### config.json
```json
{
  "SHOTGRID_API_KEY": "your_api_key",
  "SHOTGRID_SCRIPT_NAME": "webhooks",
  "SECRET_TOKEN": "your_webhook_secret",
  "SHOTGRID_URL": "https://your-site.shotgrid.autodesk.com",
  "RESILIO_URL": "https://your-resilio-console:8446",
  "RESILIO_TOKEN": "your_resilio_token"
}
```

### artists.yaml
```yaml
# Primary storage configuration
primary_storage:
  agent_name: "Linux_Remote"
  agent_id: 4
  base_path: "/home/Company"

# Target agents - where files sync to
target_agents:
  Matthew Testuser:
    agent_name: "Cameron's Mac mini"
    agent_id: 3
    base_path: "/Volumes/Company"
  Alex Trial:
    agent_name: "Cameron's MacBook Pro"
    agent_id: 2
    base_path: "/Volumes/Company"

# Path templates
path_templates:
  shots_template: "${PROJECT}/2_WORK/1_SEQUENCES/${SEQUENCE}/${SHOT}"
  assets_template: "${PROJECT}/2_WORK/2_ASSETS"

# Job settings
job_settings:
  auto_create_paths: true
  folder_permissions: "755"
  sync_direction: "bidirectional"
```

## Some Workflow Examples

### Scenario 1: Shot Becomes Active
1. **ShotGrid**: Shot TST_010_0010 status changes to "active"
2. **Webhook**: `shot_status_webhook` receives event
3. **System**: Queries all active shots and their assignments
4. **Result**: Creates `HybridWork_TST_TST_010_0010` job with assigned artists

### Scenario 2: Artist Assignment
1. **ShotGrid**: Alex assigned to TST_010_0010 Comp task
2. **Webhook**: `assignment_webhook` receives event
3. **System**: Triggers full sync for active shots
4. **Result**: Updates job to include Alex as end user

### Scenario 3: Multiple Artists, Multiple Tasks
1. **ShotGrid**:
   - Alex assigned to TST_010_0010 Comp
   - Matthew assigned to TST_010_0010 Paint
2. **System**: Aggregates all artists across all shot tasks
3. **Result**: Job includes both Alex and Matthew as end users

### Scenario 4: Artist Removal
1. **ShotGrid**: Alex removed from TST_010_0010 tasks
2. **Webhook**: `assignment_webhook` receives event
3. **System**: Detects Alex no longer assigned to shot
4. **Result**: Updates job to remove Alex, or deletes job if no artists remain

## Deployment

### Firebase Functions

Refer to Firebase Docs to login and init your firebase project.

```bash
# Deploy all functions
firebase deploy --only functions

# Deploy specific function
firebase deploy --only functions:shot_status_webhook
firebase deploy --only functions:assignment_webhook
```

### Requirements
- Firebase CLI
- Python 3.11 (complying with VFX reference platform)
- Notable dependencies: `firebase_functions`, `shotgun_api3`

## Logging & Monitoring

### Log Levels
- **INFO**: Normal operation, sync results
- **DEBUG**: Detailed payload and state information
- **WARNING**: Non-fatal errors, invalid configurations
- **ERROR**: Critical failures, API connection issues

### Key Log Messages
- `"Shot status webhook triggered - starting full Resilio sync"`
- `"Task assignment webhook triggered - triggering shot status sync"`
- `"Sync complete - Created: X shot, Y assets | Updated: X shot, Y assets | Deleted: X shot, Y assets"`
- `"Jobs to delete: X, create: Y, check for updates: Z"`

## Troubleshooting

### Common Issues
1. **Jobs not created**: Check artist configuration in `artists.yaml`
2. **Jobs not deleted**: Verify webhook signature and authentication
3. **Path issues**: Confirm path templates and agent base paths
4. **API errors**: Check Resilio Connect credentials and connectivity

### Debug Steps
1. Check Firebase Functions logs
2. Verify ShotGrid webhook delivery
3. Test API connections manually
4. Validate configuration files

---

## Full List API Functions

### Core Sync Functions

#### `sync_resilio_to_shotgrid_state(sg_state, resilio_url, resilio_token)`
Main synchronization function that ensures Resilio jobs match ShotGrid state.

**Parameters:**
- `sg_state`: Current ShotGrid state from `get_active_shots_with_assignments()`
- `resilio_url`: Resilio Connect management console URL
- `resilio_token`: Resilio Connect API token

**Returns:**
```python
{
    'shot_jobs_created': int,
    'shot_jobs_updated': int,
    'shot_jobs_deleted': int,
    'assets_jobs_created': int,
    'assets_jobs_updated': int,
    'assets_jobs_deleted': int,
    'artists_processed': int,
    'errors': [],
    'details': []
}
```

### ShotGrid State Management

#### `get_active_shots_with_assignments()`
Queries ShotGrid for all active shots and their task assignments.

**Returns:**
```python
{
    'shots': [
        {
            'id': 123,
            'code': 'TST_010_0010',
            'project': {'name': 'Test Project', 'tank_name': 'TST'},
            'sequence': 'TST_010',
            'assigned_artists': ['Matthew Testuser', 'Alex Trial']
        }
    ],
    'artist_projects': {
        'Matthew Testuser': ['TST', 'TST2'],
        'Alex Trial': ['TST']
    }
}
```

### Resilio API Functions

#### `get_all_hybrid_work_jobs()`
Retrieves all HybridWork jobs managed by the system.

**Returns:** `List[Dict[str, Any]]` - List of job objects

#### `create_job(job_attrs, ignore_errors=False)`
Creates a new hybrid work job in Resilio Connect.

**Parameters:**
- `job_attrs`: Job configuration dictionary
- `ignore_errors`: Boolean to ignore creation errors

**Returns:** `int` - Job ID

#### `update_job_agents(job_id, job_name, expected_agents)`
Updates the agent configuration for an existing job.

**Parameters:**
- `job_id`: Resilio job ID
- `job_name`: Job name for logging
- `expected_agents`: List of agent configurations

**Returns:** `bool` - Success status

#### `delete_job(job_id)`
Deletes a job from Resilio Connect.

**Parameters:**
- `job_id`: Resilio job ID

#### `compare_job_agents(job, expected_agents)`
Compares current job agents with expected configuration.

**Parameters:**
- `job`: Current job object
- `expected_agents`: Expected agent configuration

**Returns:** `bool` - True if configurations match

### Path Building Functions

#### `build_primary_storage_path(project_tank_name, sequence, shot_name)`
Constructs the primary storage path for a shot.

**Example:** `/home/Company/TST/2_WORK/1_SEQUENCES/TST_010/TST_010_0010`

#### `build_target_agent_path(artist, project_tank_name, sequence, shot_name)`
Constructs the target agent path for an artist and shot.

**Example:** `/Volumes/Company/TST/2_WORK/1_SEQUENCES/TST_010/TST_010_0010`

#### `build_primary_assets_path(project_tank_name)`
Constructs the primary storage assets path.

**Example:** `/home/Company/TST/2_WORK/2_ASSETS`

#### `build_target_assets_path(artist, project_tank_name)`
Constructs the target agent assets path.

**Example:** `/Volumes/Company/TST/2_WORK/2_ASSETS`
