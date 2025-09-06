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

    def create_hybrid_work_job(self, name: str, agent_id: int, path: str,
                              description: str = "") -> Dict[str, Any]:
        """Create a hybrid work job for a single agent."""
        try:
            job_attrs = {
                'name': name,
                'type': 'hybrid_work',  # Assuming this is the correct type
                'description': description,
                'groups': [{
                    'id': None,  # Will be auto-created
                    'agents': [{'id': agent_id}],
                    'path': {
                        'linux': path,
                        'win': path.replace('/', '\\'),
                        'osx': path
                    },
                    'permission': 'rw'
                }]
            }

            job_id = self._create_job(job_attrs)
            return {'id': job_id, 'name': name, 'path': path}

        except ApiError as e:
            raise ApiError(f"Failed to create hybrid work job '{name}': {e}")

    def update_job_path(self, job_id: int, new_path: str):
        """Update the path for an existing job."""
        try:
            # Get current job configuration
            job = self._get_job(job_id)
            groups = job.get('groups', [])

            # Update the path for all groups
            for group in groups:
                if group.get('path'):
                    group['path'] = {
                        'linux': new_path,
                        'win': new_path.replace('/', '\\'),
                        'osx': new_path
                    }

            self._update_job(job_id, {'groups': groups})

        except ApiError as e:
            raise ApiError(f"Failed to update job {job_id} path: {e}")

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
            active_shots = self.sg.find(
                "Shot",
                [["sg_status_list", "is", "active"]],
                ["id", "code", "project", "tasks"]
            )

            shots_data = []
            artist_projects = {}

            for shot in active_shots:
                project = shot.get("project", {})
                project_name = project.get("name", "")
                tank_name = project.get("tank_name", "")

                if not tank_name:
                    logger.warning(f"Shot {shot['code']} project has no tank_name, skipping")
                    continue

                # Extract sequence from shot code (TST_010_0010 -> TST_010)
                shot_code = shot.get("code", "")
                sequence = "_".join(shot_code.split("_")[:2]) if "_" in shot_code else shot_code

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

    def get_artist_agent_mapping(self) -> Dict[str, str]:
        """Get mapping of artist names to agent names from config."""
        return self.config.get("artists", {})

    def get_base_paths(self) -> Dict[str, str]:
        """Get base path templates from config."""
        paths = self.config.get("paths", {})
        return {
            'shots': paths.get('shots_template', '/Volumes/Company/${PROJECT}/2_WORK/1_SEQUENCES/${SEQUENCE}/${SHOT}'),
            'assets': paths.get('assets_template', '/Volumes/Company/${PROJECT}/2_WORK/2_ASSETS')
        }

    def build_shot_path(self, project_tank_name: str, sequence: str, shot_name: str) -> str:
        """Build full path for a shot."""
        template = self.get_base_paths()['shots']
        return template.replace('${PROJECT}', project_tank_name) \
                      .replace('${SEQUENCE}', sequence) \
                      .replace('${SHOT}', shot_name)

    def build_assets_path(self, project_tank_name: str) -> str:
        """Build full path for project assets."""
        template = self.get_base_paths()['assets']
        return template.replace('${PROJECT}', project_tank_name)

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
        artist_agents = self.get_artist_agent_mapping()

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

        # Process shot-specific jobs
        for shot in sg_state['shots']:
            project_tank = shot['project']['tank_name']
            shot_code = shot['code']
            sequence = shot['sequence']

            for artist in shot['assigned_artists']:
                if artist not in artist_agents:
                    logger.info(f"Artist {artist} not in config, skipping")
                    continue

                agent_name = artist_agents[artist]
                agent = api.find_agent_by_name(agent_name)

                if not agent:
                    error_msg = f"Agent {agent_name} for artist {artist} not found in Resilio"
                    logger.warning(error_msg)
                    results['errors'].append(error_msg)
                    continue

                try:
                    # Generate paths and job names
                    shot_path = self.build_shot_path(project_tank, sequence, shot_code)
                    job_name = self.generate_job_names(artist, project_tank, shot_code)

                    # Check if job exists
                    existing_jobs = api.find_jobs_by_pattern(job_name)

                    if existing_jobs:
                        # Update existing job
                        job = existing_jobs[0]
                        job_id = job['id']
                        api.update_job_path(job_id, shot_path)
                        results['shot_jobs_updated'] += 1
                        action = 'updated'
                    else:
                        # Create new job
                        job_result = api.create_hybrid_work_job(
                            name=job_name,
                            agent_id=agent['id'],
                            path=shot_path,
                            description=f"Shot {shot_code} for {artist}"
                        )
                        job_id = job_result['id']
                        results['shot_jobs_created'] += 1
                        action = 'created'

                    # Start job and hydrate shot folder
                    active_run = api.get_active_run_for_job(job_id)
                    if not active_run:
                        run_id = api.start_job(job_id)
                    else:
                        run_id = active_run['id']

                    # Hydrate the shot folder
                    hydrate_result = api.hydrate_files(
                        run_id=run_id,
                        files=[shot_path],
                        agents=[agent['id']]
                    )

                    success_count = sum(1 for a in hydrate_result.get("agents", [])
                                      if a.get("status") == "sent")
                    if success_count > 0:
                        results['shot_jobs_hydrated'] += 1

                    results['artists_processed'].add(artist)
                    results['details'].append({
                        'type': 'shot',
                        'artist': artist,
                        'project': project_tank,
                        'shot': shot_code,
                        'job_name': job_name,
                        'path': shot_path,
                        'action': action,
                        'hydrated': success_count > 0
                    })

                except Exception as e:
                    error_msg = f"Failed to process shot job for {artist}/{shot_code}: {e}"
                    logger.error(error_msg)
                    results['errors'].append(error_msg)

        # Process assets jobs (one per artist per project)
        for artist, projects in sg_state['artist_projects'].items():
            if artist not in artist_agents:
                continue

            agent_name = artist_agents[artist]
            agent = api.find_agent_by_name(agent_name)

            if not agent:
                continue

            for project_tank in projects:
                try:
                    assets_path = self.build_assets_path(project_tank)
                    job_name = self.generate_job_names(artist, project_tank)

                    # Check if assets job exists
                    existing_jobs = api.find_jobs_by_pattern(job_name)

                    if existing_jobs:
                        # Update existing assets job
                        job = existing_jobs[0]
                        job_id = job['id']
                        api.update_job_path(job_id, assets_path)
                        results['assets_jobs_updated'] += 1
                        action = 'updated'
                    else:
                        # Create new assets job
                        job_result = api.create_hybrid_work_job(
                            name=job_name,
                            agent_id=agent['id'],
                            path=assets_path,
                            description=f"Assets for {project_tank} - {artist}"
                        )
                        results['assets_jobs_created'] += 1
                        action = 'created'

                    results['details'].append({
                        'type': 'assets',
                        'artist': artist,
                        'project': project_tank,
                        'job_name': job_name,
                        'path': assets_path,
                        'action': action,
                        'hydrated': False  # No hydration for assets
                    })

                except Exception as e:
                    error_msg = f"Failed to process assets job for {artist}/{project_tank}: {e}"
                    logger.error(error_msg)
                    results['errors'].append(error_msg)

        # Convert set to count for JSON serialization
        results['artists_processed'] = len(results['artists_processed'])

        return results
