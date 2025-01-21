import networkx as nx
from datetime import datetime
import aiohttp
import asyncio
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
import json
import os

@dataclass
class CommitNode:
    sha: str
    message: str
    author: str
    date: datetime
    files_changed: List[str]
    parent_shas: List[str]
    children_shas: List[str]
    analysis: str
    files_count: int
    is_initial: bool
    code: Optional[str] = None

class RepositoryAnalyzer:
    def __init__(self, github_token: str, openai_key: str):
        self.github_token = github_token
        self.openai_key = openai_key
        self.headers = {
            'Authorization': f'Bearer {github_token}',
            'Accept': 'application/vnd.github+json'
        }
        self.commit_graph = nx.DiGraph()
        self.commit_summaries = []

    async def _fetch_commits(self, session: aiohttp.ClientSession, owner: str, repo: str, limit: int) -> List[Dict]:
        """Fetches commit history from GitHub API"""
        url = f'https://api.github.com/repos/{owner}/{repo}/commits?per_page={limit}'
        async with session.get(url, headers=self.headers) as response:
            if response.status != 200:
                error_data = await response.text()
                raise Exception(f'Failed to fetch commits: {error_data}')
            return await response.json()

    def _save_commit_summaries(self, repo_name: str, author_name: str):
        """Saves commit summaries to a JSON file"""
        filename = f"{repo_name}_{author_name}.json"
        
        # Convert datetime objects to ISO format strings for JSON serialization
        summaries = self.commit_summaries.copy()
        
        # Ensure the summaries directory exists
        os.makedirs('commit_summaries', exist_ok=True)
        filepath = os.path.join('commit_summaries', filename)
        
        with open(filepath, 'w') as f:
            json.dump(summaries, f, indent=2)
        
        print(f"Saved commit summaries to {filepath}")

    async def analyze_repository(self, owner: str, repo: str, limit: int = 50) -> Tuple[nx.DiGraph, List[Dict]]:
        """
        Analyzes a repository and returns both the graph and commit summaries.
        """
        try:
            async with aiohttp.ClientSession() as session:
                # Reset data structures
                self.commit_graph = nx.DiGraph()
                self.commit_summaries = []
                
                # Fetch commits
                commits = await self._fetch_commits(session, owner, repo, limit)
                
                if not commits:
                    print(f"No commits found for repository {owner}/{repo}")
                    return self.commit_graph, []
                
                # Build initial graph structure
                for commit in commits:
                    sha = commit['sha']
                    parent_shas = [p['sha'] for p in commit['parents']]
                    
                    # Add nodes and edges
                    self.commit_graph.add_node(sha, 
                        message=commit['commit']['message'],
                        author=commit['commit']['author']['name'],
                        date=datetime.fromisoformat(commit['commit']['author']['date'].replace('Z', '+00:00')),
                        is_initial=len(parent_shas) == 0
                    )
                    
                    # Add edges from parents to this commit
                    for parent_sha in parent_shas:
                        if parent_sha in [c['sha'] for c in commits]:
                            self.commit_graph.add_edge(parent_sha, sha)
                
                # Analyze commits and update both graph and summaries
                await self._analyze_commits(session, commits)
                
                # Save commit summaries to JSON file
                if commits and len(commits) > 0:
                    repo_name = repo
                    # Use the most frequent author as the filename author
                    author_counts = {}
                    for commit in commits:
                        author = commit['commit']['author']['name']
                        author_counts[author] = author_counts.get(author, 0) + 1
                    main_author = max(author_counts.items(), key=lambda x: x[1])[0]
                    self._save_commit_summaries(repo_name, main_author)
                
                return self.commit_graph
                
        except Exception as e:
            print(f"Error analyzing repository: {str(e)}")
            return nx.DiGraph()

    async def _fetch_commit_code(self, session: aiohttp.ClientSession, url: str) -> str:
        """Fetches the code content for a commit"""
        async with session.get(url, headers=self.headers) as response:
            if response.status != 200:
                return "No code available"
            data = await response.json()
            if data.get('files'):
                return data['files'][0].get('patch', "No code changes available")[:500]
            return "No code changes available"

    async def _analyze_commits(self, session: aiohttp.ClientSession, commits: List[Dict]):
        """Analyzes commits in parallel using asyncio"""
        tasks = [self._analyze_single_commit(session, commit) for commit in commits]
        await asyncio.gather(*tasks)

    async def _analyze_single_commit(self, session: aiohttp.ClientSession, commit: Dict):
        """Analyzes a single commit's changes and updates both graph and summaries"""
        sha = commit['sha']
        url = commit['url']
        
        async with session.get(url, headers=self.headers) as response:
            if response.status != 200:
                return
            
            commit_data = await response.json()
            files = commit_data.get('files', [])
            
            # Get code sample
            code_sample = await self._fetch_commit_code(session, url)
            
            # Get AI analysis
            analysis = await self._analyze_with_gpt4(files) if files else "No changes"
            
            # Update graph node
            self.commit_graph.nodes[sha].update({
                'files_changed': [f['filename'] for f in files],
                'files_count': len(files),
                'analysis': analysis,
                'code': code_sample
            })
            
            # Add to commit summaries
            commit_summary = {
                "author": commit['commit']['author']['name'],
                "code": code_sample,
                "explanation": analysis,
                "sha": sha,
                "files edited": len(files)
            }
            self.commit_summaries.append(commit_summary)

    async def _analyze_with_gpt4(self, files: List[Dict]) -> str:
        """Analyzes file changes using GPT-4"""
        patches = [f.get('patch', 'No changes available')[:1000] for f in files]
        prompt = f"Analyze this code change briefly:\nFiles modified: {', '.join(f['filename'] for f in files)}\nChanges: {' '.join(patches)}\nProvide a concise summary."
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                'https://api.openai.com/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {self.openai_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'gpt-4',
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0.7,
                    'max_tokens': 100
                }
            ) as response:
                if response.status != 200:
                    return "Analysis failed"
                result = await response.json()
                return result['choices'][0]['message']['content']