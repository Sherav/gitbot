import aiohttp
import asyncio
import functools
import gidgethub.aiohttp as gh
from sys import version_info
from typing import Optional, Callable, Any, Literal
from gidgethub import BadRequest, QueryError
from datetime import date, datetime
from itertools import cycle
from lib.structs import DirProxy, GhProfileData, TypedCache, CacheSchema, CaseInsensitiveSnakeCaseDict
from lib.utils.decorators import normalize_repository, validate_github_name
from lib.typehints import GitHubRepository, GitHubOrganization, GitHubUser

YEAR_START: str = f'{date.today().year}-01-01T00:00:30Z'
BASE_URL: str = 'https://api.github.com'
DISCORD_UPLOAD_SIZE_THRESHOLD_BYTES: int = int(7.85 * (1024 ** 2))  # 7.85mb


def github_cached(func: Callable) -> Callable:
    @functools.wraps(func)
    async def wrapper(*args: tuple, **kwargs: dict) -> Any:
        cache_key: str = f'{id(func)}:{args[1] if args else next(iter(kwargs))}'
        if cached := GitHubAPI.github_object_cache.get(cache_key):
            return cached
        result: Any = await func(*args, **kwargs)
        if isinstance(result, (dict, list)):
            GitHubAPI.github_object_cache[cache_key] = result
        return result

    return wrapper


class GitHubAPI:
    github_object_cache: TypedCache = TypedCache(CacheSchema(key=str, value=(dict, list)), maxsize=64, max_age=450)

    """
    The main class used to interact with the GitHub API.

    Parameters
    ----------
    tokens: list
        The GitHub access tokens to send requests with.
    requester: str
        A :class:`str` denoting the author of the requests (ex. 'BigNoob420')
    """

    def __init__(self, tokens: tuple, session: aiohttp.ClientSession, requester: str):
        requester += '; Python {v.major}.{v.minor}.{v.micro}'.format(v=version_info)
        self.requester: str = requester
        self.__tokens: tuple = tokens
        self.__token_cycle: cycle = cycle(t for t in self.__tokens if t is not None)
        self.queries: DirProxy = DirProxy('./resources/queries/', ('.gql', '.graphql'))
        self.setup_done: bool = False
        self.session: aiohttp.ClientSession = session
        self.gh: gh.GitHubAPI = gh.GitHubAPI(session=self.session, requester=self.requester, oauth_token=self.__token)

    @property
    def __token(self) -> str:
        return next(self.__token_cycle)

    async def ghprofile_stats(self, name: str) -> Optional[GhProfileData]:
        if '/' in name or '&' in name:
            return None
        res = await (await self.session.get(f'https://api.ghprofile.me/historic/view?username={name}')).json()
        period: dict = dict(res['payload']['period'])
        if not res['success'] or sum(int(v) for v in period.values()) == 0:
            return None
        return GhProfileData(*[int(v) for v in period.values()])

    async def get_ratelimit(self) -> tuple[tuple[dict, ...], int]:
        results: list = []
        for token in self.__tokens:
            data = await (await self.session.get('https://api.github.com/rate_limit',
                                                 headers={'Authorization': f'token {token}'})).json()
            results.append(data)
        return tuple(results), len(self.__tokens)

    async def getitem(self, resource: str, default: Any = None) -> Any:
        try:
            return await self.gh.getitem(resource)
        except BadRequest:
            return default

    @github_cached
    @validate_github_name('user')
    async def get_user_repos(self, user: GitHubUser) -> Optional[list[dict]]:
        return [
            r
            for r in await self.getitem(f'/users/{user}/repos', [])
            if r['private'] is False
        ]

    @github_cached
    @validate_github_name('org')
    async def get_org(self, org: GitHubOrganization) -> Optional[dict]:
        return await self.getitem(f'/orgs/{org}')

    @github_cached
    @validate_github_name('org', default=[])
    async def get_org_repos(self, org: GitHubOrganization) -> list[dict]:
        return [
            r
            for r in await self.getitem(f'/orgs/{org}/repos', [])
            if r['private'] is False
        ]

    @normalize_repository
    async def get_tree_file(self, repo: GitHubRepository, path: str | None = None, ref: str | None = None) -> dict | list | None:
        if repo.count('/') != 1:
            return None
        if path:
            if path[0] != '/':
                path = f'/{path}'
        else:
            path = ''
        return await self.getitem(f'/repos/{repo}/contents{path}' + (f'?ref={ref}' if ref else ''))

    @github_cached
    @validate_github_name('user', default=[])
    async def get_user_orgs(self, user: GitHubUser) -> list[dict]:
        return await self.getitem(f'/users/{user}/orgs', [])

    @github_cached
    @validate_github_name('org', default=[])
    async def get_org_members(self, org: GitHubOrganization) -> list[dict]:
        return await self.getitem(f'/orgs/{org}/public_members', [])

    @github_cached
    async def get_gist(self, gist_id: str) -> Optional[dict]:
        return await self.getitem(f'/gists/{gist_id}')

    @github_cached
    @validate_github_name('user')
    async def get_user_gists(self, user: GitHubUser):
        try:
            data = await self.gh.graphql(self.queries.user_gists, **{'Login': user})
        except QueryError:
            return None

        return data['user']

    @normalize_repository
    async def get_latest_commit(self, repo: GitHubRepository) -> Optional[dict] | Literal[False]:
        split: list = repo.split('/')
        if len(split) == 2:
            owner: str = split[0]
            repository: str = split[1]
            try:
                data: dict = await self.gh.graphql(self.queries.latest_commit, **{'Name': repository, 'Owner': owner})
            except QueryError as e:
                return False if 'Repository' in str(e) else None
            return data['repository']['defaultBranchRef']['target']

    @normalize_repository
    async def get_commit(self, repo: GitHubRepository, oid: str) -> Optional[dict] | Literal[False]:
        split: list = repo.split('/')
        if len(split) == 2:
            owner: str = split[0]
            repository: str = split[1]
            try:
                data: dict = await self.gh.graphql(self.queries.commit,
                                                   **{'Name': repository, 'Owner': owner, 'Oid': oid})
            except QueryError as e:
                return False if 'Repository' in str(e) else None
            return data['repository']['object']

    @normalize_repository
    async def get_latest_commits(self, repo: GitHubRepository, ref: Optional[str] = None) -> list[dict] | str:
        split: list = repo.split('/')
        if len(split) == 2:
            owner: str = split[0]
            repository: str = split[1]
            try:
                key: str = 'defaultBranchRef'
                if not ref:
                    data = await self.gh.graphql(self.queries.latest_commits_from_default_ref,
                                                 **{'Name': repository, 'Owner': owner, 'First': 10})
                else:
                    key: str = 'ref'
                    data = await self.gh.graphql(self.queries.latest_commits_from_ref,
                                                 **{'Name': repository, 'Owner': owner, 'RefName': ref, 'First': 10})
            except QueryError as e:
                return 'repo' if 'Repository' in str(e) else 'ref'
            if 'defaultBranchRef' not in data.get('repository', {}) and 'ref' not in data['repository']:
                return 'ref'
            try:
                return data['repository'][key]['target']['history']['nodes']
            except (TypeError, KeyError):
                return []

    @normalize_repository
    async def get_repo_zip(self,
                           repo: GitHubRepository,
                           size_threshold: int = DISCORD_UPLOAD_SIZE_THRESHOLD_BYTES) -> Optional[bool | bytes]:
        if '/' not in repo or repo.count('/') > 1:
            return None
        res = await self.session.get(
            f'{BASE_URL}/repos/{repo}/zipball',
            headers={'Authorization': f'token {self.__token}'},
        )
        if res.status == 200:
            try:
                await res.content.readexactly(size_threshold)
            except asyncio.IncompleteReadError as read:
                return read.partial
            else:
                return False
        return None

    @normalize_repository
    async def get_latest_release(self, repo: GitHubRepository) -> Optional[dict]:
        if len(_split := repo.split('/')) == 2:
            owner, name = _split
        else:
            return None

        try:
            data: dict = await self.gh.graphql(self.queries.release, **{'Name': name, 'Owner': owner})
        except QueryError:
            return None

        data = data['repository']
        data['release'] = data['releases']['nodes'][0] if data['releases']['nodes'] else None
        data['color'] = int(data['primaryLanguage']['color'][1:], 16) if data['primaryLanguage'] else 0x2f3136
        del data['primaryLanguage']
        del data['releases']
        return data

    @normalize_repository
    @github_cached
    async def get_repo(self, repo: GitHubRepository) -> Optional[dict]:
        split: list = repo.split('/')
        if len(split) == 2:
            owner: str = split[0]
            repository: str = split[1]

            try:
                data: dict = await self.gh.graphql(self.queries.repo, **{'Name': repository, 'Owner': owner})
            except QueryError:
                return None

            data = data['repository']
            data['languages'] = data['languages']['totalCount']
            data['topics'] = (data['repositoryTopics']['nodes'], data['repositoryTopics']['totalCount'])
            data['graphic'] = data['openGraphImageUrl'] if data['usesCustomOpenGraphImage'] else None
            data['release'] = data['releases']['nodes'][0]['tagName'] if data['releases']['nodes'] else None
            return data

    @normalize_repository
    @github_cached
    async def rest_get_repo(self, repo: GitHubRepository) -> Optional[dict]:
        try:
            data: dict = await self.gh.getitem(f'/repos/{repo}')
        except BadRequest:
            return None

        return data

    @normalize_repository
    async def get_pull_request(self,
                               repo: GitHubRepository,
                               number: int,
                               data: Optional[dict] = None) -> dict | str:
        if not data:
            if repo.count('/') != 1:
                return 'repo'
            split: list = repo.split('/')
            owner: str = split[0]
            repository: str = split[1]

            try:
                data = await self.gh.graphql(self.queries.pull_request, **{'Name': repository,
                                                                           'Owner': owner,
                                                                           'Number': number})
            except QueryError as e:
                return 'number' if 'number' in str(e) else 'repo'
        data: dict = data['repository']['pullRequest'] if 'repository' in data else data
        data['labels']: list = [lb['node']['name'] for lb in data['labels']['edges']]
        data['assignees']['users'] = [(u['node']['login'], u['node']['url']) for u in data['assignees']['edges']]
        data['reviewers'] = {}
        data['reviewers']['users'] = [
            (o['node']['requestedReviewer']['login'] if 'login' in o['node']['requestedReviewer'] else
             o['node']['requestedReviewer']['name'], o['node']['requestedReviewer']['url']) for o
            in data['reviewRequests']['edges']]
        data['reviewers']['totalCount'] = data['reviewRequests']['totalCount']
        data['participants']['users'] = [(u['node']['login'], u['node']['url']) for u in
                                         data['participants']['edges']]
        return data

    @normalize_repository
    async def get_last_pull_requests_by_state(self,
                                              repo: GitHubRepository,
                                              last: int = 10,
                                              state: str = 'OPEN') -> Optional[list[dict]]:
        if repo.count('/') != 1:
            return None

        split: list = repo.split('/')
        owner: str = split[0]
        repository: str = split[1]

        try:
            data: dict = await self.gh.graphql(self.queries.pull_requests, **{'Name': repository,
                                                                              'Owner': owner,
                                                                              'States': state,
                                                                              'Last': last})
        except QueryError:
            return None
        return data['repository']['pullRequests']['nodes']

    @normalize_repository
    async def get_issue(self,
                        repo: GitHubRepository,
                        number: int,
                        data: Optional[dict] = None,  # If data isn't None, this method simply acts as a parser
                        had_keys_removed: bool = False) -> dict | str:
        if not data:
            if repo.count('/') != 1:
                return 'repo'

            split: list = repo.split('/')
            owner: str = split[0]
            repository: str = split[1]

            try:
                data: dict = await self.gh.graphql(self.queries.issue, **{'Name': repository,
                                                                          'Owner': owner,
                                                                          'Number': number})
            except QueryError as e:
                return 'number' if 'number' in str(e) else 'repo'
        if isinstance(data, dict):
            if not had_keys_removed:
                data: dict = data['repository']['issue']
            comment_count: int = data['comments']['totalCount']
            assignee_count: int = data['assignees']['totalCount']
            participant_count: int = data['participants']['totalCount']
            del data['comments']
            data['body']: str = data['bodyText']
            del data['bodyText']
            data['commentCount']: int = comment_count
            data['assigneeCount']: int = assignee_count
            data['participantCount']: int = participant_count
            data['labels']: list = [lb['name'] for lb in list(data['labels']['nodes'])]
        return data

    @normalize_repository
    async def get_last_issues_by_state(self,
                                       repo: GitHubRepository,
                                       last: int = 10,
                                       state: str = 'OPEN') -> Optional[list[dict]]:
        if repo.count('/') != 1:
            return None
        split: list = repo.split('/')
        owner: str = split[0]
        repository: str = split[1]

        try:
            data: dict = await self.gh.graphql(self.queries.issues, **{'Name': repository,
                                                                       'Owner': owner,
                                                                       'States': state,
                                                                       'Last': last})
        except QueryError:
            return None
        return data['repository']['issues']['nodes']

    @github_cached
    @validate_github_name('user')
    async def get_user(self, user: GitHubUser) -> Optional[dict]:
        try:
            data = await self.gh.graphql(self.queries.user, **{'Login': user,
                                                               'FromTime': YEAR_START,
                                                               'ToTime': datetime.utcnow().strftime('%Y-%m-%dT%XZ')})
        except QueryError:
            return None
        data_ = data['user']['contributionsCollection']['contributionCalendar']
        data['user']['contributions'] = data_['totalContributions'], data_['weeks'][-1]['contributionDays'][-1][
            'contributionCount']
        data = data['user']
        del data['contributionsCollection']
        data['organizations'] = data['organizations']['totalCount']
        data['public_repos'] = data['repositories']['totalCount']
        data['following'] = data['following']['totalCount']
        data['followers'] = data['followers']['totalCount']
        return CaseInsensitiveSnakeCaseDict(data)
