"""
Resilio State Synchronization Manager
Ensures Resilio Connect hybrid work jobs always match ShotGrid assignments and shot statuses.
"""
import os
import re
import yaml
from typing import Dict, Any, Optional, List, Set, Tuple
from api import ApiBaseCommands
from errors import ApiError
import logging

logger = logging.getLogger("resilio-state-sync")


class ResilioStateAPI(ApiBaseCommands):
    """Extended Resilio API for state management operations."""

    def __init__(self, base_url: str, token: str, verify: bool = False):
        super().__init__(base_url, token, verify)

    def find_jobs_by_pattern(self, pattern: str) -> List[Dict[str, Any]]:
        """Find jobs by name pattern (supports basic wildcard matching)."""
        try:
            jobs = self._get_jobs()
            matching_jobs = []

            # Convert pattern to regex (basic * wildcard support)
            regex_pattern = pattern.replace("*", ".*")
            regex_pattern = f"^{regex_pattern}$"

            for job in jobs:
                job_name = job.get("name", "")
                if re.match(regex_pattern, job_name, re.IGNORECASE):
                    matching_jobs.append(job)

            return matching_jobs
        except ApiError:
            return []

    def find_agent_by_name(self, agent_name: str) -> Optional[Dict[str, Any]]:
        """Find an agent by name."""
        try:
            agents = self._get_agents()
            for agent in agents:
                if agent.get("name", "").lower() == agent_name.lower():
                    return agent
            return None
        except ApiError:
            return None

    def get_active_run_for_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        """Get the currently active run for a job, if any."""
        try:
            runs = self._get_job_runs({"job_id": job_id})
            for run in runs:
                status = run.get("status", "").lower()
                if status in ["running", "active", "in_progress"]:
                    return run
            return None
        except ApiError:
            return None

    def create_hybrid_work_job(self, name: str, primary_storage_agent_id: str, primary_storage_path: str,
                              target_agent_id: str, target_agent_path: str, description: str = "") -> Dict[str, Any]:
        """Create a hybrid work job with specific paths for primary and target storage."""
        try:
            job_attrs = {
                'name': name,
                'type': 'hybrid_work',
                'description': description,
                'agents': [
                    {
                        'id': primary_storage_agent_id,  # Remove int() conversion
                        'role': 'primary_storage',
                        'permission': 'rw',
                        'priority_agents': True,
                        'path': {
                            'linux': primary_storage_path,
                            'win': primary_storage_path.replace('/', '\\'),
                            'osx': primary_storage_path
                        }
                    },
                    {
                        'id': target_agent_id,  # Remove int() conversion
                        'role': 'enduser',
                        'permission': 'srw',
                        'file_policy_id': 1,
                        'path': {
                            'linux': target_agent_path,
                            'win': target_agent_path.replace('/', '\\'),
                            'osx': target_agent_path
                        }
                    }
                ]
            }

            job_id = self._create_job(job_attrs, ignore_errors=True)
            return {'id': job_id, 'name': name}

        except ApiError as e:
            raise ApiError(f"Failed to create hybrid work job '{name}': {e}")


    def update_hybrid_work_job_paths(self, job_id: int, primary_storage_path: str, target_agent_path: str):
        """Update the paths for an existing hybrid work job."""
        try:
            # Get current job configuration
            job = self._get_job(job_id)

            # Remove read-only properties that cause API errors
            read_only_props = ['total_transferred', 'created_at', 'created_by', 'last_start_time', 'errors', 'access', 'notifications']
            for prop in read_only_props:
                job.pop(prop, None)

            # Update agent paths in the agents array
            if 'agents' in job:
                for agent in job['agents']:
                    if agent.get('role') == 'primary_storage':
                        agent['path'] = {
                            'linux': primary_storage_path,
                            'win': primary_storage_path.replace('/', '\\'),
                            'osx': primary_storage_path
                        }
                    elif agent.get('role') == 'enduser':
                        agent['path'] = {
                            'linux': target_agent_path,
                            'win': target_agent_path.replace('/', '\\'),
                            'osx': target_agent_path
                        }

            self._update_job(job_id, job)

        except ApiError as e:
            raise ApiError(f"Failed to update job {job_id} paths: {e}")


    def hydrate_files(self, run_id: int, files: List[str],
                     agents: Optional[List[int]] = None) -> Dict[str, Any]:
        """Hydrate files for a specific job run."""
        if len(files) > 1000:
            raise ApiError("Maximum 1000 files per request")

        payload = {"files": files}
        if agents:
            payload["agents"] = agents

        try:
            response = self._put(f"/runs/{run_id}/files/hydrate", json=payload)
            return response.json()
        except Exception as e:
            raise ApiError(f"Failed to hydrate files for run {run_id}: {e}")

    def delete_job_if_exists(self, job_name: str) -> bool:
        """Delete a job by name if it exists."""
        try:
            jobs = self._get_jobs()
            for job in jobs:
                if job.get("name") == job_name:
                    job_id = job.get("id")
                    self._delete_job(job_id)
                    logger.info(f"Deleted job: {job_name}")
                    return True
            return False
        except ApiError as e:
            logger.error(f"Failed to delete job {job_name}: {e}")
            return False

    def start_job(self, job_id: int) -> int:
        """Start a job by creating a job run."""
        try:
            run_attrs = {"job_id": job_id}
            job_run_id = self._create_job_run(run_attrs)
            return job_run_id
        except ApiError as e:
            raise ApiError(f"Failed to start job {job_id}: {e}")

class ShotGridStateManager:
    """Manages querying ShotGrid for current assignment and shot state."""

    def __init__(self, sg_client):
        self.sg = sg_client

    def get_active_shots_with_assignments(self) -> Dict[str, Any]:
        """
        Get all active shots and their task assignments.

        Returns:
            {
                'shots': [
                    {
                        'id': 123,
                        'code': 'TST_010_0010',
                        'project': {'name': 'Test Project', 'tank_name': 'TST'},
                        'sequence': 'TST_010',
                        'assigned_artists': ['Matthew', 'Alex']
                    }
                ],
                'artist_projects': {
                    'Matthew': ['TST', 'TST2'],
                    'Alex': ['TST']
                }
            }
        """
        try:
            # Get all active shots
            # Get all active shots
            active_shots = self.sg.find(
                "Shot",
                [["sg_status_list", "is", "active"]],
                ["id", "code", "project", "tank_name", "sg_sequence", "tasks"]  # Add sg_sequence
            )

            # Debug: Check the specific shot that triggered this
            debug_shot = self.sg.find_one(
                "Shot",
                [["id", "is", 1213]],
                ["id", "code", "sg_status_list", "project.Project.tank_name", "project"]
            )
            logger.info(f"Debug shot 1213: {debug_shot}")

            # Debug: Check if we found any active shots at all
            logger.info(f"Active shots query returned {len(active_shots)} shots")
            if active_shots:
                for shot in active_shots[:3]:  # Log first 3 shots
                    logger.info(f"Active shot found: ID={shot.get('id')}, code={shot.get('code')}, status={shot.get('sg_status_list')}")


            shots_data = []
            artist_projects = {}

            for shot in active_shots:
                project = shot.get("project", {})
                project_id = project.get("id")
                project_name = project.get("name", "")

                # Fetch full project details including tank_name
                tank_name = ""
                if project_id:
                    try:
                        full_project = self.sg.find_one(
                            "Project",
                            [["id", "is", project_id]],
                            ["name", "tank_name"]
                        )
                        if full_project:
                            tank_name = full_project.get("tank_name", "")
                            if not project_name:  # Use project name from full fetch if missing
                                project_name = full_project.get("name", "")
                    except Exception as e:
                        logger.warning(f"Failed to fetch project details for project {project_id}: {e}")

                if not tank_name:
                    logger.warning(f"Shot {shot['code']} project '{project_name}' has no tank_name, skipping")
                    continue

                logger.info(f"Processing shot {shot['code']} with tank_name: {tank_name}")

                # NEW CODE - Use ShotGrid sequence field instead of parsing shot code
                shot_code = shot.get("code", "")

                # Get the actual sequence from ShotGrid
                sg_sequence = shot.get("sg_sequence", {})
                if sg_sequence and isinstance(sg_sequence, dict):
                    sequence = sg_sequence.get("name", "VFX")
                else:
                    sequence = "VFX"  # Default fallback

                logger.info(f"Shot {shot_code} is in sequence: {sequence}")

                # Get tasks for this shot to find assigned artists
                tasks = self.sg.find(
                    "Task",
                    [["entity", "is", {"type": "Shot", "id": shot["id"]}]],
                    ["task_assignees", "sg_status_list"]
                )

                assigned_artists = set()
                for task in tasks:
                    assignees = task.get("task_assignees", [])
                    for assignee in assignees:
                        artist_name = assignee.get("name", "")
                        if artist_name:
                            assigned_artists.add(artist_name)

                            # Track which projects each artist works on
                            if artist_name not in artist_projects:
                                artist_projects[artist_name] = set()
                            artist_projects[artist_name].add(tank_name)

                shots_data.append({
                    'id': shot['id'],
                    'code': shot_code,
                    'project': {
                        'name': project_name,
                        'tank_name': tank_name
                    },
                    'sequence': sequence,
                    'assigned_artists': list(assigned_artists)
                })

            # Convert sets to lists for JSON serialization
            for artist in artist_projects:
                artist_projects[artist] = list(artist_projects[artist])

            return {
                'shots': shots_data,
                'artist_projects': artist_projects
            }

        except Exception as e:
            logger.error(f"Failed to query ShotGrid state: {e}")
            return {'shots': [], 'artist_projects': {}}


class ResilioStateSyncManager:
    """
    Main sync manager that ensures Resilio jobs match ShotGrid state.
    """

    def __init__(self, config_path: str = "artists.yaml"):
        self.config_path = config_path
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """Load YAML configuration file."""
        with open(self.config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def build_primary_storage_path(self, project_tank_name: str, sequence: str, shot_name: str) -> str:
        """Build full path for primary storage."""
        primary_base = self.config.get("primary_storage", {}).get("base_path", "")
        template = self.config.get("path_templates", {}).get("shots_template", "")

        relative_path = template.replace('${PROJECT}', project_tank_name) \
                              .replace('${SEQUENCE}', sequence) \
                              .replace('${SHOT}', shot_name)

        return f"{primary_base}/{relative_path}"

    def build_target_agent_path(self, artist: str, project_tank_name: str, sequence: str, shot_name: str) -> str:
        """Build full path for target agent."""
        target_config = self.config.get("target_agents", {}).get(artist, {})
        target_base = target_config.get("base_path", "")
        template = self.config.get("path_templates", {}).get("shots_template", "")

        relative_path = template.replace('${PROJECT}', project_tank_name) \
                              .replace('${SEQUENCE}', sequence) \
                              .replace('${SHOT}', shot_name)

        return f"{target_base}/{relative_path}"

    def build_primary_assets_path(self, project_tank_name: str) -> str:
        """Build full path for project assets on primary storage."""
        primary_base = self.config.get("primary_storage", {}).get("base_path", "")
        template = self.config.get("path_templates", {}).get("assets_template", "")

        relative_path = template.replace('${PROJECT}', project_tank_name)
        return f"{primary_base}/{relative_path}"

    def build_target_assets_path(self, artist: str, project_tank_name: str) -> str:
        """Build full path for project assets on target agent."""
        target_config = self.config.get("target_agents", {}).get(artist, {})
        target_base = target_config.get("base_path", "")
        template = self.config.get("path_templates", {}).get("assets_template", "")

        relative_path = template.replace('${PROJECT}', project_tank_name)
        return f"{target_base}/{relative_path}"

    def get_primary_storage_agent_id(self, api: ResilioStateAPI) -> str:
        """Get the primary storage agent ID."""
        primary_agent_id = self.config.get("primary_storage", {}).get("agent_id", "")
        if primary_agent_id:
            return primary_agent_id

        # Fallback to name lookup if ID not configured
        primary_agent_name = self.config.get("primary_storage", {}).get("agent_name", "")
        agent = api.find_agent_by_name(primary_agent_name)
        if not agent:
            raise ApiError(f"Primary storage agent '{primary_agent_name}' not found")
        return agent['id']

    def get_target_agent_id(self, api: ResilioStateAPI, artist: str) -> str:
        """Get the target agent ID for an artist."""
        target_config = self.config.get("target_agents", {}).get(artist, {})
        agent_id = target_config.get("agent_id", "")
        if agent_id:
            return agent_id

        # Fallback to name lookup if ID not configured
        agent_name = target_config.get("agent_name", "")
        agent = api.find_agent_by_name(agent_name)
        if not agent:
            raise ApiError(f"Target agent '{agent_name}' for artist '{artist}' not found")
        return agent['id']

    def generate_job_names(self, artist: str, project: str, shot: str = None) -> str:
        """Generate standardized job names."""
        if shot:
            return f"HybridWork_{artist}_{project}_{shot}"
        else:
            return f"HybridWork_{artist}_{project}_Assets"

    def sync_resilio_to_shotgrid_state(self, sg_state: Dict[str, Any],
                                     resilio_url: str, resilio_token: str) -> Dict[str, Any]:
        """
        Synchronize Resilio jobs to match ShotGrid state.

        Args:
            sg_state: Output from ShotGridStateManager.get_active_shots_with_assignments()
            resilio_url: Resilio Connect URL
            resilio_token: API token

        Returns:
            Sync results summary
        """
        api = ResilioStateAPI(resilio_url, resilio_token, verify=False)

        # Get primary storage agent ID once
        try:
            primary_storage_agent_id = self.get_primary_storage_agent_id(api)
        except ApiError as e:
            return {
                'shot_jobs_created': 0,
                'shot_jobs_updated': 0,
                'shot_jobs_hydrated': 0,
                'assets_jobs_created': 0,
                'assets_jobs_updated': 0,
                'artists_processed': 0,
                'errors': [f"Failed to get primary storage agent: {e}"],
                'details': []
            }

        results = {
            'shot_jobs_created': 0,
            'shot_jobs_updated': 0,
            'shot_jobs_hydrated': 0,
            'assets_jobs_created': 0,
            'assets_jobs_updated': 0,
            'artists_processed': set(),
            'errors': [],
            'details': []
        }

        # Process shot-specific jobs (GROUP BY SHOT, not by artist)
        processed_shots = set()

        for shot in sg_state['shots']:
            project_tank = shot['project']['tank_name']
            shot_code = shot['code']
            sequence = shot['sequence']

            # Skip if we already processed this shot
            shot_key = f"{project_tank}_{shot_code}"
            if shot_key in processed_shots:
                continue
            processed_shots.add(shot_key)

            # Get all valid artists for this shot
            valid_artists = [
                artist for artist in shot['assigned_artists']
                if artist in self.config.get("target_agents", {})
            ]

            if not valid_artists:
                logger.info(f"No valid artists configured for shot {shot_code}, skipping")
                continue

            try:
                # Build primary storage path (shared by all artists)
                primary_path = self.build_primary_storage_path(project_tank, sequence, shot_code)

                # Build end-user agents array
                end_user_agents = []
                for artist in valid_artists:
                    target_agent_id = self.get_target_agent_id(api, artist)
                    target_path = self.build_target_agent_path(artist, project_tank, sequence, shot_code)

                    end_user_agents.append({
                        'id': target_agent_id,
                        'role': 'enduser',
                        'permission': 'srw',
                        'file_policy_id': 1,
                        'path': {
                            'linux': target_path,
                            'win': target_path.replace('/', '\\'),
                            'osx': target_path
                        }
                    })

                # Job name without artist names
                job_name = f"HybridWork_{project_tank}_{shot_code}"

                # Check if job exists
                existing_jobs = api.find_jobs_by_pattern(job_name)

                if existing_jobs:
                    # Update existing job (complex - would need to compare agents)
                    logger.info(f"Job {job_name} already exists, skipping update for now")
                    continue
                else:
                    # Create new job with all artists as end users
                    job_attrs = {
                        'name': job_name,
                        'type': 'hybrid_work',
                        'description': f"Shot {shot_code} for: {', '.join(valid_artists)}",
                        'agents': [
                            {
                                'id': primary_storage_agent_id,
                                'role': 'primary_storage',
                                'permission': 'rw',
                                'priority_agents': True,
                                'path': {
                                    'linux': primary_path,
                                    'win': primary_path.replace('/', '\\'),
                                    'osx': primary_path
                                }
                            }
                        ] + end_user_agents
                    }

                    job_id = api._create_job(job_attrs, ignore_errors=True)
                    results['shot_jobs_created'] += 1
                    results['artists_processed'].update(valid_artists)

                    results['details'].append({
                        'type': 'shot',
                        'artists': valid_artists,
                        'project': project_tank,
                        'shot': shot_code,
                        'job_name': job_name,
                        'primary_path': primary_path,
                        'action': 'created'
                    })

            except Exception as e:
                error_msg = f"Failed to process shot job for {shot_code}: {e}"
                logger.error(error_msg)
                results['errors'].append(error_msg)

        # Process assets jobs (ONE JOB PER PROJECT with multiple artists)
        processed_asset_projects = set()

        for artist, projects in sg_state['artist_projects'].items():
            if artist not in self.config.get("target_agents", {}):
                continue

            for project_tank in projects:
                # Skip if we already processed this project's assets
                if project_tank in processed_asset_projects:
                    continue

                # Get all artists working on this project
                project_artists = [
                    a for a, projs in sg_state['artist_projects'].items()
                    if project_tank in projs and a in self.config.get("target_agents", {})
                ]

                if not project_artists:
                    continue

                processed_asset_projects.add(project_tank)

                try:
                    # Build primary assets path (shared by all artists)
                    primary_assets_path = self.build_primary_assets_path(project_tank)

                    # Build end-user agents array for assets
                    asset_end_user_agents = []
                    for project_artist in project_artists:
                        target_agent_id = self.get_target_agent_id(api, project_artist)
                        target_assets_path = self.build_target_assets_path(project_artist, project_tank)

                        asset_end_user_agents.append({
                            'id': target_agent_id,
                            'role': 'enduser',
                            'permission': 'srw',
                            'file_policy_id': 1,
                            'path': {
                                'linux': target_assets_path,
                                'win': target_assets_path.replace('/', '\\'),
                                'osx': target_assets_path
                            }
                        })

                    # Assets job name without artist names
                    assets_job_name = f"HybridWork_{project_tank}_Assets"

                    # Check if assets job exists
                    existing_jobs = api.find_jobs_by_pattern(assets_job_name)

                    if existing_jobs:
                        logger.info(f"Assets job {assets_job_name} already exists, skipping update for now")
                        continue
                    else:
                        # Create new assets job with all artists as end users
                        assets_job_attrs = {
                            'name': assets_job_name,
                            'type': 'hybrid_work',
                            'description': f"Assets for {project_tank} - Artists: {', '.join(project_artists)}",
                            'agents': [
                                {
                                    'id': primary_storage_agent_id,
                                    'role': 'primary_storage',
                                    'permission': 'rw',
                                    'priority_agents': True,
                                    'path': {
                                        'linux': primary_assets_path,
                                        'win': primary_assets_path.replace('/', '\\'),
                                        'osx': primary_assets_path
                                    }
                                }
                            ] + asset_end_user_agents
                        }

                        assets_job_id = api._create_job(assets_job_attrs, ignore_errors=True)
                        results['assets_jobs_created'] += 1
                        results['artists_processed'].update(project_artists)

                        results['details'].append({
                            'type': 'assets',
                            'artists': project_artists,
                            'project': project_tank,
                            'job_name': assets_job_name,
                            'primary_path': primary_assets_path,
                            'action': 'created'
                        })

                except Exception as e:
                    error_msg = f"Failed to process assets job for project {project_tank}: {e}"
                    logger.error(error_msg)
                    results['errors'].append(error_msg)


        # Convert set to count for JSON serialization
        results['artists_processed'] = len(results['artists_processed'])

        return results
