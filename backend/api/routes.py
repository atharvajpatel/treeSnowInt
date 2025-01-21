from fastapi import APIRouter, HTTPException
from typing import Dict, Optional
import os
from pydantic import BaseModel
from analyzer.repo_analyzer import RepositoryAnalyzer
from analyzer.graph_processor import GraphProcessor
from dotenv import load_dotenv
import traceback
import logging
from fastapi.security import APIKeyHeader
from fastapi import Security, Depends
import aiohttp

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Create router
router = APIRouter()

# Security
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def verify_api_key(api_key: str = Security(api_key_header)):
    """Verify API key if one is set in environment"""
    expected_key = os.getenv("API_KEY")
    if expected_key:
        if not api_key or api_key != expected_key:
            raise HTTPException(
                status_code=403,
                detail="Invalid API key"
            )
    return api_key

class RepositoryRequest(BaseModel):
    owner: str
    repo: str
    limit: Optional[int] = 50

class DiffRequest(BaseModel):
    owner: str
    repo: str
    commit: str
    file: str

class OpenAIKeyResponse(BaseModel):
    key: str

@router.get("/api/v1/config/openai-key", response_model=OpenAIKeyResponse)
async def get_openai_key(api_key: str = Depends(verify_api_key)):
    """
    Returns a configured OpenAI key for frontend use.
    Requires API key authentication if configured.
    """
    try:
        openai_key = os.getenv("OPENAI_API_KEY")
        if not openai_key:
            raise HTTPException(
                status_code=500,
                detail="OpenAI API key not configured on server"
            )
        
        return OpenAIKeyResponse(key=openai_key)
    
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Error fetching OpenAI key: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve OpenAI key"
        )

@router.post("/api/v1/analyze")
async def analyze_repository(
    request: RepositoryRequest,
    api_key: str = Depends(verify_api_key)
):
    """
    Analyzes a GitHub repository and returns the processed data
    for visualization
    """
    try:
        # Get tokens from environment variables
        github_token = os.getenv("GITHUB_TOKEN")
        openai_key = os.getenv("OPENAI_API_KEY")

        if not github_token or not openai_key:
            logger.error("Missing API keys in server configuration")
            raise HTTPException(
                status_code=500,
                detail="Missing API keys in server configuration"
            )

        # Initialize analyzer
        analyzer = RepositoryAnalyzer(
            github_token=github_token,
            openai_key=openai_key
        )

        # Analyze repository
        logger.info(f"Analyzing repository: {request.owner}/{request.repo}")
        try:
            graph = await analyzer.analyze_repository(
                request.owner,
                request.repo,
                request.limit
            )
            
            if not graph or graph.number_of_nodes() == 0:
                logger.error("No nodes found in analyzed repository")
                raise HTTPException(
                    status_code=404,
                    detail="No commits found in repository"
                )
            
            logger.info(f"Graph created with {graph.number_of_nodes()} nodes")
            
            # Process graph for visualization
            processor = GraphProcessor(graph)
            visualization_data = processor.process_for_visualization()
            
            # Add OpenAI key to visualization data
            visualization_data['config'] = {
                'openai_key': openai_key
            }
            
            # Validate visualization data
            if not visualization_data.get('nodes') or not visualization_data.get('edges'):
                logger.error("Invalid visualization data structure")
                raise HTTPException(
                    status_code=500,
                    detail="Failed to process repository data"
                )
            
            logger.info(f"Processed {len(visualization_data['nodes'])} nodes and {len(visualization_data['edges'])} edges")
            logger.info(f"Sample node data: {visualization_data['nodes'][0] if visualization_data['nodes'] else 'No nodes'}")
            
            return visualization_data
            
        except Exception as e:
            logger.error(f"Error during analysis: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise HTTPException(
                status_code=500,
                detail=f"Analysis failed: {str(e)}"
            )
            
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail=f"Server error: {str(e)}"
        )

@router.post("/api/v1/diff")
async def get_file_diff(
    request: DiffRequest,
    api_key: str = Depends(verify_api_key)
):
    """
    Retrieves the diff content for a specific file in a commit
    """
    try:
        github_token = os.getenv("GITHUB_TOKEN")
        if not github_token:
            raise HTTPException(
                status_code=500,
                detail="GitHub token not configured on server"
            )

        headers = {
            'Authorization': f'Bearer {github_token}',
            'Accept': 'application/vnd.github.v3.diff'
        }

        # First, get the commit details to find parent
        async with aiohttp.ClientSession() as session:
            commit_url = f'https://api.github.com/repos/{request.owner}/{request.repo}/commits/{request.commit}'
            async with session.get(commit_url, headers=headers) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch commit: {await response.text()}")
                    raise HTTPException(
                        status_code=response.status,
                        detail="Failed to fetch commit details"
                    )
                commit_data = await response.json()

            # Get the specific file diff
            diff_url = f'https://api.github.com/repos/{request.owner}/{request.repo}/commits/{request.commit}'
            async with session.get(diff_url, headers=headers) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch diff: {await response.text()}")
                    raise HTTPException(
                        status_code=response.status,
                        detail="Failed to fetch diff"
                    )
                
                full_diff = await response.text()
                
                # Parse the full diff to extract the specific file's diff
                file_diffs = parse_diff(full_diff)
                requested_diff = file_diffs.get(request.file)
                
                if not requested_diff:
                    logger.info(f"No changes found for file: {request.file}")
                    return {"content": "No changes found for this file"}
                
                return {"content": requested_diff}

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Error fetching diff: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get diff: {str(e)}"
        )

def parse_diff(diff_content: str) -> Dict[str, str]:
    """Parse a git diff and return a dictionary of filename -> diff content"""
    files = {}
    current_file = None
    current_content = []
    
    for line in diff_content.split('\n'):
        if line.startswith('diff --git'):
            # Save previous file content if exists
            if current_file and current_content:
                files[current_file] = '\n'.join(current_content)
            
            # Start new file
            current_content = [line]
            # Extract filename from diff --git line
            # Format is typically: diff --git a/path/to/file b/path/to/file
            try:
                current_file = line.split(' b/')[-1]
            except:
                current_file = None
        elif current_file:
            current_content.append(line)
    
    # Add the last file
    if current_file and current_content:
        files[current_file] = '\n'.join(current_content)
    
    return files