# -*- coding: utf-8 -*-
"""
Search tools for the Audnexus Plex plugin.

This module provides comprehensive search functionality for finding audiobook metadata:
- ASIN detection from filenames, ID3 tags, and manual search queries
- ISBN detection and fallback search when ASIN is not available
- API URL construction for Audnexus and Audible APIs with multi-region support
- Result parsing and scoring using Levenshtein distance
- Series detection and extraction for improved search accuracy
- Edition/subtitle parsing for distinguishing between audiobook versions

Classes:
    SearchTool: Base class for search functionality
    AlbumSearchTool: Search tool for audiobook albums (books)
    ArtistSearchTool: Search tool for audiobook artists (authors)
    ScoreTool: Scoring tool for ranking search results
    SearchConfig: Configuration constants for scoring thresholds and weights

Exceptions:
    SearchToolError: Base exception for search-related errors
    ASINNotFoundError: Raised when ASIN cannot be found
    APIResponseError: Raised when API response is invalid
    RegionNotSupportedError: Raised for unsupported region codes
    MetadataExtractionError: Raised when metadata extraction fails

Note:
    This module is designed for Python 2.7 compatibility (Plex's embedded Python).
    Type hints use comment syntax for Python 2.7 compatibility.
"""
# plex debugging
try:
    import plexhints  # noqa: F401
except ImportError:
    pass
else:  # the code is running outside of Plex
    from plexhints.util_kit import String  # util kit
    from plexhints.prefs_kit import Prefs  # prefs kit
    from plexhints.agent_kit import Media  # agent kit

# Import reduce for both Plex and non-Plex environments
# In Python 2.7, reduce is a builtin, but we import from functools for consistency
# and future Python 3 compatibility
try:
    from functools import reduce
except ImportError:
    pass  # Python 2.7 has reduce as builtin

# Python 2/3 compatibility for unicode type
# In Python 3, str is unicode; in Python 2, there's a separate unicode type
try:
    unicode
except NameError:
    # Python 3: unicode doesn't exist, str is already unicode
    unicode = str

from datetime import date
import re
# Import internal tools
from audnexuslogging import Logging
from region_tools import RegionTool
import urllib
from asin_id3 import build_region_asin

# Setup logger
log = Logging()


# -----------------------------------------------------------------------------
# Custom Exceptions
# Standardized exception hierarchy for error handling
# -----------------------------------------------------------------------------
class SearchToolError(Exception):
    """
    Base exception for all search tool errors.

    All custom exceptions in the search_tools module inherit from this class,
    allowing calling code to catch all search-related errors with a single
    except clause if desired.

    Example:
        >>> try:
        ...     tool.check_for_asin()
        ... except SearchToolError as e:
        ...     log.error('Search failed: %s', e)
    """
    pass


class ASINNotFoundError(SearchToolError):
    """
    Raised when ASIN cannot be found in any expected location.

    This exception is raised when the search tool exhausts all
    methods of finding an ASIN (filename, ID3 tags, manual search)
    without success.
    """
    pass


class APIResponseError(SearchToolError):
    """
    Raised when API response is invalid or cannot be parsed.

    This exception indicates that the API returned data that
    could not be processed, such as malformed JSON, missing
    required fields, or unexpected response structure.

    Attributes:
        response: The raw response that caused the error
    """
    def __init__(self, message, response=None):
        super(APIResponseError, self).__init__(message)
        self.response = response


class RegionNotSupportedError(SearchToolError):
    """
    Raised when an unsupported region is specified.

    Valid regions are: us, uk, au, ca, de, es, fr, in, it, jp
    """
    pass


class MetadataExtractionError(SearchToolError):
    """
    Raised when metadata extraction fails.

    This can occur when reading ID3 tags or parsing filename
    patterns for ASIN/ISBN.
    """
    pass

# -----------------------------------------------------------------------------
# Pre-compiled regex patterns (compiled once at module load for efficiency)
# -----------------------------------------------------------------------------
# ASIN pattern: 10 alphanumeric characters that contain at least one digit
asin_regex = re.compile(r'(?=.\d)[A-Z\d]{10}')
# Region override pattern: 2-letter code in square brackets e.g., [uk], [us]
region_regex = re.compile(r'(?<=\[)[A-Za-z]{2}(?=\])')
# ISBN pattern: 10 or 13 digit ISBN with optional X check digit
isbn_regex = re.compile(r'^(?:\d{9}[\dX]|97[89]\d{10})$')
# Contributor tag pattern: matches text before " -" contributor suffix
contributor_regex = re.compile(r'.+?(?= -)')
# Patterns for cleaning series/search strings
book_number_regex = re.compile(r'\b(book|volume|part|episode)\s*\d+\b', re.IGNORECASE)
standalone_number_regex = re.compile(r'\b\d+\b')
special_chars_regex = re.compile(r'[^\w\s]')
multi_whitespace_regex = re.compile(r'\s+')
# Unwanted words in titles
unwanted_words_regex = re.compile(
    r'\b(official|audiobook|unabridged|abridged)\b', 
    re.IGNORECASE
)
# Roman numeral pattern for series detection
roman_numeral_regex = re.compile(
    r'\b(I{1,3}|IV|V|VI{0,3}|IX|X{1,3}|XI{0,3}|XII|XIII|XIV|XV)\b',
    re.IGNORECASE
)

# -----------------------------------------------------------------------------
# Edition/Subtitle Detection Patterns
# Used to extract edition info that helps distinguish between versions
# -----------------------------------------------------------------------------
# Edition keywords: matches edition qualifiers like "Deluxe Edition", "Uncut"
edition_regex = re.compile(
    r'\b(deluxe|unabridged|abridged|complete|uncut|extended|anniversary|'
    r'revised|updated|expanded|special|collectors?|definitive|original|'
    r'new|remastered|full cast|dramatized|graphic audio)\s*(?:edition|version)?\b',
    re.IGNORECASE
)
# Subtitle separator: matches common subtitle separators (colon, dash with spaces)
# This is more specific than the series patterns to avoid false positives
subtitle_separator_regex = re.compile(r'^(.+?)\s*:\s+(.+)$')

# -----------------------------------------------------------------------------
# Series Detection Patterns
# Multiple patterns to catch various series naming conventions
# -----------------------------------------------------------------------------
SERIES_PATTERNS = [
    # Pattern 1: "Title - Series Name" or "Title: Subtitle"
    re.compile(r'^(.+?)\s*[-:]\s*(.+)$'),
    # Pattern 2: "Title (Series Name)" or "Title (Book 1)"
    re.compile(r'^(.+?)\s*\(([^)]+)\)\s*$'),
    # Pattern 3: "Title, Book 1 of Series" or "Title, Volume 2"
    re.compile(r'^(.+?),?\s*[Bb]ook\s*(\d+)(?:\s*of\s*(.+))?$'),
    # Pattern 4: "Series #1: Title" or "Series #12 - Title"
    re.compile(r'^(.+?)\s*#(\d+)(?:\s*[-:]\s*(.+))?$'),
    # Pattern 5: "Title Book 1" or "Title Volume 2" (no separator)
    re.compile(r'^(.+?)\s+(?:[Bb]ook|[Vv]olume|[Pp]art)\s*(\d+)\s*$'),
]


# -----------------------------------------------------------------------------
# Configuration Constants
# Centralized configuration for scoring thresholds and weights
# -----------------------------------------------------------------------------
class SearchConfig:
    """
    Configuration constants for search and scoring operations.

    Centralizes all magic numbers and thresholds for easy tuning.
    These values can be adjusted to change the sensitivity and
    behavior of the search matching algorithm.

    Score Calculation:
        final_score = INITIAL_SCORE - (weighted_deductions) - result_index

    Result Filtering:
        Results with final_score < IGNORE_SCORE are filtered out

    Attributes:
        INITIAL_SCORE (int): Starting score before deductions (100)
        IGNORE_SCORE (int): Minimum score to include in results (45)
        ALBUM_SCORE_WEIGHT (int): Multiplier for album title mismatch (2)
        AUTHOR_SCORE_WEIGHT (int): Multiplier for author mismatch (10)
        LANGUAGE_MISMATCH_PENALTY (int): Points deducted for language mismatch (2)
        YEAR_PENALTY_PER_YEAR (int): Points deducted per year of difference (2)
        YEAR_PENALTY_CAP (int): Maximum year-based penalty (10)
        EDITION_MATCH_BONUS (int): Bonus for matching edition (-5)
        EDITION_PARTIAL_BONUS (int): Bonus for partial edition match (-3)
        EDITION_MISSING_PENALTY (int): Penalty if expected edition not found (3)
        NO_METADATA_PENALTY (int): Penalty when media lacks album metadata (50)
    """
    # Score thresholds
    INITIAL_SCORE = 100
    IGNORE_SCORE = 45

    # Score weights (higher = more impactful)
    ALBUM_SCORE_WEIGHT = 2
    AUTHOR_SCORE_WEIGHT = 10

    # Penalties
    LANGUAGE_MISMATCH_PENALTY = 2
    YEAR_PENALTY_PER_YEAR = 2
    YEAR_PENALTY_CAP = 10
    NO_METADATA_PENALTY = 50

    # Edition scoring (negative = bonus)
    EDITION_MATCH_BONUS = -5
    EDITION_PARTIAL_BONUS = -3
    EDITION_MISSING_PENALTY = 3


# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------
def build_url_param(key, value, prefix='&'):
    """
    Build a URL-encoded query parameter string.

    Args:
        key (str): The parameter name (e.g., 'title', 'author', 'keywords')
        value (str): The parameter value to be URL-encoded
        prefix (str): The prefix for the parameter ('&' by default, use '' for first param)

    Returns:
        str: URL-encoded parameter string (e.g., '&author=Stephen%20King')
             Returns empty string if value is None or empty.

    Example:
        >>> build_url_param('author', 'Stephen King')
        '&author=Stephen%20King'
        >>> build_url_param('title', 'The Stand', prefix='')
        'title=The%20Stand'
    """
    if value:
        return prefix + key + '=' + urllib.quote(value.encode('utf-8') if isinstance(value, unicode) else value)
    return ''


def clean_search_string(text):
    """
    Clean a string for use in search queries.

    Removes book numbers, standalone numbers, special characters, and normalizes whitespace.
    This is commonly needed when preparing series info or search keywords.

    Args:
        text (str): The input string to clean

    Returns:
        str: Cleaned string suitable for search queries

    Example:
        >>> clean_search_string('Stone Barrington - Book 15')
        'Stone Barrington'
        >>> clean_search_string('Harry Potter, Volume 1: The Beginning')
        'Harry Potter The Beginning'
    """
    if not text:
        return ''
    # Remove book/volume/part numbers (e.g., "Book 15")
    result = book_number_regex.sub('', text)
    # Remove standalone numbers
    result = standalone_number_regex.sub('', result)
    # Remove special characters (keep alphanumeric and spaces)
    result = special_chars_regex.sub('', result)
    # Normalize whitespace
    result = multi_whitespace_regex.sub(' ', result).strip()
    return result


def extract_series_info(name):
    """
    Extract title and series information from album/book name.

    Uses multiple regex patterns to detect various series naming conventions:
    - "Title - Series Name" or "Title: Subtitle"
    - "Title (Series Name)" or "Title (Book 1)"
    - "Title, Book 1 of Series"
    - "Series #1: Title"
    - "Title Book 1" or "Title Volume 2"

    Args:
        name (str): The album/book name to parse

    Returns:
        tuple: (primary_title, series_info, book_number)
            - primary_title (str): The main title, extracted from series
            - series_info (str or None): Series name or extra info
            - book_number (str or None): Book number if detected

    Example:
        >>> extract_series_info('Hot Mahogany - Stone Barrington 15')
        ('Hot Mahogany', 'Stone Barrington 15', None)
        >>> extract_series_info('The Fellowship of the Ring (The Lord of the Rings, Book 1)')
        ('The Fellowship of the Ring', 'The Lord of the Rings, Book 1', None)
        >>> extract_series_info('Dune')
        ('Dune', None, None)
    """
    if not name:
        return (name, None, None)

    # Try each pattern in order of specificity
    for pattern in SERIES_PATTERNS:
        match = pattern.match(name)
        if match:
            groups = match.groups()
            if len(groups) >= 2:
                primary = groups[0].strip() if groups[0] else name
                # Skip if primary title is too short (likely false positive)
                if len(primary) <= 3:
                    continue
                series = groups[1].strip() if len(groups) > 1 and groups[1] else None
                book_num = groups[2] if len(groups) > 2 else None
                # Combine series info if book number is separate
                if book_num and series:
                    series = series + ' ' + str(book_num) if book_num else series
                log.debug('Series pattern matched: title="%s", series="%s"', primary, series)
                return (primary, series, book_num)

    # No pattern matched, return original
    return (name, None, None)


def extract_edition_info(name):
    """
    Extract subtitle and edition information from an album/book name.

    Edition information helps distinguish between different versions of the same
    audiobook (e.g., "Dune: Deluxe Edition" vs "Dune: Unabridged"). This data
    is stored separately for weighted matching to improve search accuracy.

    Detects:
    - Edition keywords: deluxe, unabridged, complete, remastered, etc.
    - Subtitles: Text after colons (e.g., "Title: Subtitle")
    - Format indicators: full cast, dramatized, graphic audio

    Args:
        name (str): The album/book name to parse

    Returns:
        tuple: (edition, subtitle)
            - edition (str or None): Edition descriptor if found
            - subtitle (str or None): Subtitle text if found

    Example:
        >>> extract_edition_info('Dune: Deluxe Edition')
        ('deluxe edition', None)
        >>> extract_edition_info('The Stand: Complete & Uncut')
        ('complete uncut', None)
        >>> extract_edition_info('Project Hail Mary: A Novel')
        (None, 'A Novel')
        >>> extract_edition_info('1984')
        (None, None)
    """
    if not name:
        return (None, None)

    edition = None
    subtitle = None

    # Check for edition keywords anywhere in the name
    edition_match = edition_regex.search(name)
    if edition_match:
        edition = edition_match.group(0).strip().lower()
        log.debug('Found edition info: %s', edition)

    # Check for subtitle after colon (but not if it's an edition)
    subtitle_match = subtitle_separator_regex.match(name)
    if subtitle_match:
        potential_subtitle = subtitle_match.group(2).strip()
        # Only consider it a subtitle if it's NOT an edition keyword
        if not edition_regex.search(potential_subtitle):
            subtitle = potential_subtitle
            log.debug('Found subtitle: %s', subtitle)

    return (edition, subtitle)


# -----------------------------------------------------------------------------
# Region Fallback Configuration
# Maps regions to fallback options prioritized by language compatibility
# -----------------------------------------------------------------------------
REGION_FALLBACK_MAP = {
    # English-language regions (highest compatibility with each other)
    'us': ['uk', 'au', 'ca'],
    'uk': ['us', 'au', 'ca'],
    'au': ['uk', 'us', 'ca'],
    'ca': ['us', 'uk', 'au'],

    # Non-English regions (fall back to English as secondary)
    'de': ['us', 'uk'],
    'es': ['us', 'uk'],
    'fr': ['us', 'uk'],
    'it': ['us', 'uk'],
    'in': ['uk', 'us'],  # India - UK English first
    'jp': ['us'],
}


def get_fallback_regions(primary_region):
    """
    Get a list of fallback regions to try if primary region yields no results.

    Returns regions prioritized by language compatibility with the primary
    region. This allows finding audiobooks that may only be available in
    certain regional markets.

    Args:
        primary_region (str): The primary region code (e.g., 'us', 'uk', 'de')

    Returns:
        list: Ordered list of fallback region codes to try

    Example:
        >>> get_fallback_regions('de')
        ['us', 'uk']
        >>> get_fallback_regions('au')
        ['uk', 'us', 'ca']
    """
    return REGION_FALLBACK_MAP.get(primary_region, ['us'])


class SearchTool(object):
    """
    Base class for search functionality in the Audnexus Plex plugin.

    Provides common methods for ASIN detection, region handling, URL building,
    and API response parsing. Subclasses (AlbumSearchTool, ArtistSearchTool)
    implement content-type specific logic.

    Attributes:
        content_type (str): Type of content ('books' or 'authors')
        lang (str): Language code for the library
        manual (bool): Whether this is a manual search
        media: Plex media object with album/artist metadata
        prefs: Plex preferences object
        results (list): List to populate with search results
        region_override (str): Region code override for API calls
        logger: Optional custom logger for testing (default: module logger)
    """

    def __init__(self, content_type, lang, manual, media, prefs, results, logger=None):
        # type: (str, str, bool, object, object, list, object) -> None
        """
        Initialize the SearchTool.

        Args:
            content_type (str): Type of content ('books' or 'authors')
            lang (str): Language code for the library
            manual (bool): Whether this is a manual search
            media: Plex Media.Album or Media.Artist object
            prefs: Plex Prefs object
            results (list): List to populate with search results
            logger: Optional custom logger for dependency injection in tests.
                   Defaults to module-level logger if not provided.

        Note:
            The logger parameter enables unit testing by allowing mock loggers
            to be injected. In production, omit this parameter to use the
            default Plex logging system.
        """
        self.content_type = content_type
        self.lang = lang
        self.manual = manual
        self.media = media
        self.prefs = prefs
        self.results = results
        # Allows custom logger injection for testing
        self.log = logger if logger is not None else log

    def build_url(self, query, backup=False):
        """
            Generates the URL string with search paramaters for API call.
        """
        # Pre-process title. If ASIN is found, return the URL
        pre_process = self.pre_process_title()
        if pre_process:
            return pre_process

        # Setup region helper to get search URL
        region_helper = RegionTool(
            content_type=self.content_type, query=query, region=self.region_override)
        if backup:
            search_url = region_helper.backup_api.get_search_url()
        else:
            search_url = region_helper.get_search_url()
        self.log_search_url(search_url)
        return search_url

    def build_url_for_region(self, query, region, backup=False):
        """
        Generate a search URL for a specific region.

        Used by multi-region fallback search to try different regional APIs
        when the primary region doesn't return good results.

        Args:
            query (str): URL-encoded query parameters
            region (str): Region code to use (e.g., 'us', 'uk', 'de')
            backup (bool): If True, use the backup API for this region

        Returns:
            str: The constructed search URL for the specified region
        """
        region_helper = RegionTool(
            content_type=self.content_type, query=query, region=region)
        if backup:
            return region_helper.backup_api.get_search_url()
        return region_helper.get_search_url()

    def get_fallback_regions_for_search(self):
        """
        Get list of fallback regions to try if primary search yields poor results.

        Returns regions prioritized by language compatibility with the current
        region_override setting.

        Returns:
            list: Ordered list of fallback region codes
        """
        return get_fallback_regions(self.region_override)

    def check_for_asin(self):
        """
        Check filename (for books) and/or search query for ASIN to enable quick matching.

        This method performs a multi-stage check for ASIN (Amazon Standard Identification
        Number) in the following order:
        1. Filename - For books, checks the media filename for embedded ASIN
        2. ID3 tags - If no ASIN in filename, checks audio file metadata tags
        3. Manual search - Checks album/artist field for user-entered ASIN

        For GraphicAudio content (region_override == 'GA'), ISBN is searched instead of ASIN.

        Returns:
            str or None: ASIN/ISBN with region suffix (e.g., 'B08G9PRS1K_us') if found,
                        None if no identifier found.

        Side Effects:
            - Sets self.region_override via check_for_region() calls
            - Logs info/debug messages about search progress

        Example:
            >>> tool.check_for_asin()
            'B08G9PRS1K_us'  # Found ASIN in filename with US region
        """
        # Check filename for ASIN if content type is books
        if self.media.filename and self.content_type == 'books':
            result = self._check_filename_for_identifier()
            if result:
                return result

        # Check search query for ASIN (manual search)
        result = self._check_manual_search_for_asin()
        if result:
            return result

        return None

    def _check_filename_for_identifier(self):
        """
        Check filename for ASIN or ISBN identifier.

        Internal helper for check_for_asin() that handles filename parsing
        and identifier extraction.

        Returns:
            str or None: Identifier with region suffix if found, None otherwise
        """
        filename_unquoted = urllib.unquote(
            self.media.filename).decode('utf8')
        self.check_for_region(filename_unquoted)

        if self.region_override == "GA":
            return self._check_filename_for_isbn(filename_unquoted)
        else:
            return self._check_filename_for_asin_or_id3(filename_unquoted)

    def _check_filename_for_isbn(self, filename):
        """
        Check filename for ISBN (GraphicAudio content).

        Args:
            filename (str): Decoded filename to search

        Returns:
            str or None: ISBN with region suffix if found, None otherwise
        """
        filename_search_isbn = None
        try:
            filename_search_isbn = self.search_isbn(filename)
        except Exception as e:
            log.error('Error checking filename for ISBN: %s', e)

        if filename_search_isbn:
            log.info('ISBN found in filename (GA)')
            return filename_search_isbn.group(0) + '_' + self.region_override
        return None

    def _check_filename_for_asin_or_id3(self, filename):
        """
        Check filename and ID3 tags for ASIN.

        Args:
            filename (str): Decoded filename to search

        Returns:
            str or None: ASIN with region suffix if found, None otherwise
        """
        # Try filename first
        filename_search_asin = None
        try:
            filename_search_asin = self.search_asin(filename)
        except Exception as e:
            log.error('Error checking filename for ASIN: %s', e)

        if filename_search_asin:
            log.info('ASIN found in filename')
            return filename_search_asin.group(0) + '_' + self.region_override

        # Fall back to ID3 tags
        log.debug('No ASIN found in filename, checking ID3 tags')
        id3_asin = build_region_asin(filename, self.prefs['region'])
        if id3_asin:
            log.info('ASIN found in ID3 tags')
            self.check_for_region(id3_asin)
            return id3_asin

        return None

    def _check_manual_search_for_asin(self):
        """
        Check manual search query for ASIN.

        Returns:
            str or None: ASIN with region suffix if found, None otherwise
        """
        manual_asin = self.media.album if self.media.album else self.media.artist
        manual_search_asin = self.search_asin(manual_asin)

        if manual_search_asin:
            log.info('ASIN found in manual search')
            self.check_for_region(manual_asin)
            return manual_search_asin.group(0) + '_' + self.region_override

        return None

    # Check for region override
    def check_for_region(self, search_title):
        """
            Overrides the search with a region.
        """
        match_region = self.search_region(search_title)
        if match_region:
            log.info('Region found in title')
            self.region_override = match_region.group(0)
        else:
            self.region_override = self.prefs['region']
        log.info('Region Override: %s', self.region_override)

    def clear_contributor_text(self, string):
        """
        Remove contributor suffix from author/artist name.

        Contributor suffixes appear as " - Narrator", " - Author", etc.
        This method extracts just the name portion.

        Args:
            string (str): Name that may contain contributor suffix

        Returns:
            str: Name without contributor suffix, or original string if no suffix found

        Example:
            >>> tool.clear_contributor_text('John Smith - Narrator')
            'John Smith'
            >>> tool.clear_contributor_text('Jane Doe')
            'Jane Doe'
        """
        # Use pre-compiled regex for efficiency
        match = contributor_regex.match(string)
        if match:
            return match.group(0)
        return string

    def log_search_url(self, search_url):
        """
            Logs the search URL.
        """
        log.debug('Search URL: %s', search_url)

    def override_with_asin(self, match_asin, region=None, backup=False):
        """
        Override search with a specific ASIN for direct lookup.

        When an ASIN is found in the filename, ID3 tags, or manual search,
        this method builds a direct lookup URL instead of a search query.

        Args:
            match_asin: Regex match object containing the ASIN
            region (str, optional): Region code override. Defaults to prefs region.
            backup (bool): Whether to use backup API. Defaults to False.

        Returns:
            str: API URL for direct ASIN lookup

        Side Effects:
            - Sets self.region_override
            - Logs the search URL
        """
        log.debug('Overriding %s search with ASIN', self.content_type)
        asin = match_asin.group(0)
        # Param uses keyword for book and nothing for author
        type_param = '&keywords=' if self.content_type == 'books' else ''
        # Wrap the param for url use
        url_param = type_param + urllib.quote(asin)

        # Setup region helper to get search URL
        self.region_override = region if region else self.prefs['region']
        region_helper = RegionTool(
            content_type=self.content_type, query=url_param, region=self.region_override)
        if backup:
            search_url = region_helper.backup_api.get_search_url()
        else:
            # Books use api search authors use audnexus search
            if self.content_type == 'books':
                search_url = region_helper.get_search_url()
            else:
                # Set ID to ASIN
                region_helper.id = asin
                search_url = region_helper.get_id_url()

        self.log_search_url(search_url)
        return search_url

    def pre_process_title(self):
        """
        Pre-process the title to check for ASIN and prepare for search.

        This method performs pre-search processing:
        1. Determines the appropriate search title based on content type
        2. Checks for and sets region override
        3. For books, ensures the name is normalized before ASIN search
        4. If ASIN is found in the title, returns the override URL

        Returns:
            str or None: Search URL if ASIN is found in title (via override_with_asin),
                        None if no ASIN found (caller should proceed with normal search).

        Side Effects:
            - Sets self.region_override via check_for_region()
            - For books, calls normalize_name() which sets self.normalizedName

        Note:
            For books, normalize_name() must be called before accessing normalizedName.
            This method handles that initialization automatically.
        """
        log.debug('Pre-processing title')
        # Setup some basic things
        search_title = self.media.album if self.content_type == 'books' else self.media.artist
        asin_search_title = self.media.artist

        # Region override
        self.check_for_region(search_title)

        # Normalize name - must be called before accessing self.normalizedName
        # This ensures the attribute exists before we try to use it
        if self.content_type == 'books':
            # Check if normalizedName has been set, if not, call normalize_name()
            if not hasattr(self, 'normalizedName') or self.normalizedName is None:
                self.normalize_name()
            asin_search_title = self.normalizedName

        # ASIN override
        match_asin = self.search_asin(asin_search_title)
        if match_asin:
            log.debug('ASIN found in title')
            return self.override_with_asin(match_asin, self.region_override)

        # Explicit return None when no ASIN found
        # This makes the return value explicit for callers
        return None

    def search_asin(self, input):
        """
            Searches for ASIN in a string.
        """
        if input:
            return re.search(asin_regex, input)

    def search_isbn(self, input):
        """
        Search for ISBN in a string.

        Looks for ISBN-10 or ISBN-13 patterns in the input string.
        ISBN-10: 9 digits followed by digit or X
        ISBN-13: 978 or 979 prefix followed by 10 digits

        Args:
            input (str): String to search for ISBN

        Returns:
            re.Match or None: Match object if ISBN found, None otherwise
        """
        if input:
            return re.search(isbn_regex, input)

    def search_isbn_anywhere(self, input):
        """
        Search for ISBN pattern anywhere in a string (less strict).

        Unlike search_isbn() which requires the ISBN to be at start/end,
        this method finds ISBNs embedded in filenames or other text.

        Args:
            input (str): String to search for ISBN

        Returns:
            str or None: ISBN string if found, None otherwise

        Example:
            >>> tool.search_isbn_anywhere('audiobook_9780316769488_final.mp3')
            '9780316769488'
        """
        if not input:
            return None

        # More flexible ISBN pattern for embedded ISBNs
        # ISBN-10: 10 digits with optional X at end
        # ISBN-13: 13 digits starting with 978 or 979
        isbn_anywhere_pattern = r'(?:97[89]\d{10}|\d{9}[\dX])'
        match = re.search(isbn_anywhere_pattern, input)
        if match:
            return match.group(0)
        return None

    def check_for_isbn_fallback(self):
        """
        Check for ISBN in media metadata as a fallback identifier.

        When no ASIN is found, this method attempts to find an ISBN
        in the filename or metadata that could be used to search for
        the audiobook. ISBNs can sometimes be converted to ASINs via
        API lookup.

        Returns:
            str or None: ISBN if found in metadata, None otherwise

        Note:
            This extends ISBN detection beyond just GraphicAudio content.
            The ISBN can be used as a keyword search fallback when no ASIN
            match is found.
        """
        # Check filename for ISBN
        if self.media.filename and self.content_type == 'books':
            filename_unquoted = urllib.unquote(
                self.media.filename).decode('utf8')
            isbn = self.search_isbn_anywhere(filename_unquoted)
            if isbn:
                log.debug('ISBN found in filename: %s', isbn)
                return isbn

        # Check album/title for ISBN-like patterns
        title = self.media.album if self.media.album else self.media.title
        if title:
            isbn = self.search_isbn_anywhere(title)
            if isbn:
                log.debug('ISBN found in title: %s', isbn)
                return isbn

        return None

    def build_isbn_search_url(self, isbn):
        """
        Build a search URL using ISBN as keyword.

        When an ISBN is found but no ASIN, this constructs a keyword
        search that may find the audiobook through ISBN reference.

        Args:
            isbn (str): The ISBN to search for

        Returns:
            str: Search URL with ISBN as keyword parameter

        Note:
            Some audiobook entries in Audible include ISBN in their
            metadata, so keyword search can sometimes find matches.
        """
        query = build_url_param('keywords', isbn, prefix='')
        region_helper = RegionTool(
            content_type=self.content_type, query=query, region=self.region_override)
        return region_helper.get_search_url()

    def search_region(self, input):
        """
            Searches for region in a string.
        """
        if input:
            return re.search(region_regex, input)

    def validate_author_name(self):
        """
            Checks a list of known bad author names.
            If matched, author name is set to None to prevent
            it being used in search query.
        """
        if self.content_type == 'authors':
            self.get_primary_author()

        strings_to_check = [
            "[Unknown Artist]"
        ]
        for test_name in strings_to_check:
            if self.media.artist == test_name:
                self.media.artist = None
                log.info(
                    "Artist name seems to be bad, "
                    "not using it in search."
                )
                break


class AlbumSearchTool(SearchTool):
    """
    Search tool for audiobook albums (books).

    Extends SearchTool with book-specific search logic including
    title normalization, series extraction, and book metadata parsing.
    """

    def build_search_args(self):
        """
        Build the search query parameters for the API call.

        Constructs a URL query string with title, author, and optional keywords
        parameters. Extracts series information from album names for better
        search matching.

        Returns:
            str: URL-encoded query string (e.g., 'title=Dune&author=Frank%20Herbert')

        Side Effects:
            - Calls normalize_name() which sets self.normalizedName and self.series_info
        """
        # First, normalize the name (this also extracts series_info)
        self.normalize_name()

        # Add series info as keywords if extracted (use helper function)
        keywords_param = ''
        if hasattr(self, 'series_info') and self.series_info:
            series_keywords = clean_search_string(self.series_info)
            if series_keywords:
                keywords_param = build_url_param('keywords', series_keywords)
                log.debug('Adding series keywords: %s', series_keywords)

        # Fix match/manual search doesn't provide author
        if self.media.artist:
            # Build album and author params using helper function
            album_param = build_url_param('title', self.normalizedName, prefix='')
            artist_param = build_url_param('author', self.media.artist)
        else:
            # Use keyword search to supplement missing author
            album_param = build_url_param('keywords', self.normalizedName, prefix='')
            artist_param = ''
            keywords_param = ''  # Don't duplicate keywords

        # Combine params
        query = (album_param + artist_param + keywords_param)
        return query

    def check_if_preorder(self, book_date):
        """
        Check if a book is a preorder based on its release date.

        Pre-order books are excluded from search results to avoid
        matching unreleased content.

        Args:
            book_date (date): The book's release date

        Returns:
            bool: True if book is a preorder (release date in future), None otherwise
        """
        current_date = (date.today())
        if book_date > current_date:
            log.info("Excluding pre-order book")
            return True

    def name_to_initials(self, input_name):
        """
            Converts a name to initials.
            Shorten input_name by splitting on whitespaces
            Only the surname stays as whole, the rest gets truncated
            and merged with dots.
            Example: 'Arthur Conan Doyle' -> 'A.C.Doyle'
            Example: 'J K Rowling' -> 'J.K.Rowling'
            Example: 'J. R. R. Tolkien' -> 'J.R.R.Tolkien'
        """

        # Remove quotation marks
        input_name = input_name.replace('"', '')

        # Split name into parts
        name_parts = self.clear_contributor_text(input_name).split()

        # Check if prename and surname exist, otherwise exit
        if len(name_parts) < 2:
            return input_name

        new_name = ""
        # Truncate prenames
        for part in name_parts[:-1]:
            try:
                # Try to get first letter of prename and add dot
                new_name += part[0] + "." if part[1] != "." else part
            except IndexError:
                # If there is only one letter, add dot and return
                new_name += part + "." if part != "." else part
        # Add surname
        new_name += name_parts[-1]

        return new_name

    def normalize_name(self):
        """
        Normalize the album name for search query use.

        Performs the following transformations:
        1. Removes diacritics (accented characters → ASCII equivalents)
        2. Removes bracket content (e.g., [uk] region overrides)
        3. Extracts edition and subtitle info for weighted matching
        4. Extracts primary title using multiple series detection patterns
        5. Stores series info separately for keyword search fallback
        6. Removes special characters and unwanted words
        7. Normalizes whitespace

        Returns:
            str: Normalized album name

        Side Effects:
            - Sets self.normalizedName with the cleaned title
            - Sets self.series_info with extracted series information (if any)
            - Sets self.book_number with extracted book number (if any)
            - Sets self.edition with edition descriptor (if any) - e.g., 'deluxe', 'unabridged'
            - Sets self.subtitle with subtitle text (if any) - e.g., 'A Novel'

        Example:
            >>> tool.normalize_name()  # For album "Dune: Deluxe Edition [uk]"
            'Dune'  # self.edition = 'deluxe edition', self.subtitle = None
            >>> tool.normalize_name()  # For album "Hot Mahogany - Stone Barrington 15"
            'Hot Mahogany'  # self.series_info = 'Stone Barrington 15'
        """
        # Get name from either album or title
        input_name = self.media.album if self.media.album else self.media.title
        log.debug('Input Name: %s', input_name)

        # Remove Diacritics
        name = String.StripDiacritics(input_name)
        # Remove brackets and text inside (e.g., [uk] region overrides)
        name = re.sub(r'\[[^"]*\]', '', name)

        # Extract edition and subtitle info
        # This stores edition/subtitle for optional weighted matching
        edition_info, subtitle_info = extract_edition_info(name)
        self.edition = edition_info
        self.subtitle = subtitle_info

        if self.edition:
            log.debug('Extracted edition: %s', self.edition)
        if self.subtitle:
            log.debug('Extracted subtitle: %s', self.subtitle)

        # Use enhanced series detection (3.2 improvement)
        # This handles multiple patterns: "Title - Series", "Title (Book 1)", "Series #1: Title", etc.
        primary_title, series_part, book_num = extract_series_info(name)

        self.series_info = series_part
        self.book_number = book_num

        if self.series_info:
            log.debug('Extracted series info: %s', self.series_info)
        if self.book_number:
            log.debug('Extracted book number: %s', self.book_number)

        # Use primary title if extraction was successful
        if primary_title and len(primary_title) > 3:
            name = primary_title
            log.debug('Using primary title: %s', name)

        # Remove unwanted characters using pre-compiled regex
        name = special_chars_regex.sub('', name)
        # Remove unwanted words using pre-compiled regex
        name = unwanted_words_regex.sub('', name)
        # Remove unwanted whitespaces using pre-compiled regex
        name = multi_whitespace_regex.sub(' ', name)
        # Remove leading and trailing whitespaces
        name = name.strip()
        # Set class variable
        self.normalizedName = name
        log.debug('Normalized Name: %s', self.normalizedName)

        return name

    def _normalize_api_item(self, item, date_key):
        """
        Normalize a single API response item to a standard format.

        Internal helper method to avoid duplicating the dictionary creation logic.

        Args:
            item (dict): Single item from API response
            date_key (str): Key name for the date field ('release_date' or 'releaseDate')

        Returns:
            dict: Normalized result dictionary with standard keys
        """
        return {
            'asin': item['asin'] + '_' + self.region_override,
            'author': item['authors'],
            'date': item[date_key],
            'language': item['language'],
            'narrator': item['narrators'],
            'region': self.region_override,
            'title': item['title'],
        }

    def _has_required_keys(self, item, required_keys):
        """
        Check if a dictionary has all required keys.

        Python 2/3 compatible key checking helper.

        Args:
            item (dict): Dictionary to check
            required_keys (set): Set of required key names

        Returns:
            bool: True if all required keys present, False otherwise

        Note:
            Uses viewkeys() for Python 2.7 compatibility.
            For Python 3, replace with: set(item.keys()) >= required_keys
        """
        # NOTE: viewkeys() is Python 2.7 specific - use set(item.keys()) for Python 3
        return item.viewkeys() >= required_keys

    def parse_api_response(self, api_response):
        """
        Parse API response and extract relevant fields for Plex search results.

        Handles two API response formats:
        1. Audible API format: Response contains 'products' key with 'release_date' field
        2. Audnexus API format: Response is direct array with 'releaseDate' field

        Args:
            api_response (dict or list): API response - either a dict containing 'products'
                key or a direct list of book items.

        Returns:
            list: List of normalized result dictionaries containing:
                - asin: ASIN with region suffix (e.g., 'B08G9PRS1K_us')
                - author: List of author dicts with 'name' key
                - date: Release date string
                - language: Language of the audiobook
                - narrator: List of narrator dicts with 'name' key
                - region: Region code (e.g., 'us', 'uk')
                - title: Book title

        Example:
            >>> results = tool.parse_api_response(api_response)
            >>> results[0]['asin']
            'B08G9PRS1K_us'
        """
        search_results = []

        # Determine API format and normalize
        if 'products' in api_response:
            # Audible API format: products array with snake_case keys
            items = api_response['products']
            date_key = 'release_date'
            required_keys = {"asin", "authors", "language", "narrators", "release_date", "title"}
        else:
            # Audnexus API format: direct array with camelCase keys
            items = api_response
            date_key = 'releaseDate'
            required_keys = {"asin", "authors", "language", "narrators", "releaseDate", "title"}

        # Process all items with unified logic
        for item in items:
            if self._has_required_keys(item, required_keys):
                search_results.append(self._normalize_api_item(item, date_key))

        return search_results

    def pre_search_logging(self):
        """
            Logs basic metadata before search.
        """
        log.separator(msg='ALBUM SEARCH', log_level="info")
        # Log basic metadata
        data_to_log = [
            {'ID': self.media.parent_metadata.id},
            {'Title': self.media.title},
            {'Name': self.media.name},
            {'Album': self.media.album},
            {'Artist': self.media.artist},
        ]
        log.metadata(data_to_log)
        log.separator(log_level="info")

        # Handle a couple of edge cases where
        # album search will give bad results.
        if self.media.album is None and not self.manual:
            if self.media.title:
                log.warn('Using track title since album title is missing.')
                self.media.album = self.media.title
                return True
            log.info('Album Title is NULL on an automatic search.  Returning')
            return None
        if self.media.album == '[Unknown Album]' and not self.manual:
            log.info(
                'Album Title is [Unknown Album]'
                ' on an automatic search.  Returning'
            )
            return None

        if self.manual:
            # If this is a custom search,
            # use the user-entered name instead of the scanner hint.
            if self.media.name:
                log.info('Custom album search for: %s', self.media.name)
                self.media.album = self.media.name
        return True


class ArtistSearchTool(SearchTool):
    """
    Search tool for audiobook artists (authors).

    Extends SearchTool with author-specific search logic including
    name cleanup, multi-artist handling, and contributor detection.
    """

    def build_search_args(self):
        """
        Build the search query parameters for the author API call.

        Cleans up the author name and constructs a URL query string.

        Returns:
            str: URL-encoded query string (e.g., 'name=Stephen%20King')
        """
        modified_artist_name = self.cleanup_author_name(self.media.artist)
        # Use helper function for consistent URL parameter building
        query = build_url_param('name', modified_artist_name, prefix='')
        return query

    def cleanup_author_name(self, name):
        """
        Clean up author name by removing unwanted characters and titles.

        Performs the following transformations:
        1. Removes bracket content (e.g., [uk] region overrides)
        2. Removes titles (Dr., Prof., etc.)
        3. Normalizes initials (J. K. Rowling → J K Rowling)

        Args:
            name (str): Raw author name from media metadata

        Returns:
            str: Cleaned author name suitable for API search
        """
        log.debug('Artist name before cleanup: %s', name)

        # Remove brackets and text inside
        name = re.sub(r'\[[^"]*\]', '', name)
        # Remove certain strings, such as titles
        str_to_remove = [
            'Dr.',
            'EdD',
            'Prof.',
            'Professor',
        ]
        str_to_remove_regex = re.compile(
            '|'.join(map(re.escape, str_to_remove))
        )
        name = str_to_remove_regex.sub('', name)
        # Remove periods between double initials
        initials_regex = r"^((?:[A-Z]\.\s?)*[A-Z]\.(?!\S)).(\w+)"
        initials_matched = re.search(initials_regex, name)
        if initials_matched:
            log.debug('Found initials to clean')
            cleaned_initials = (
                initials_matched.group(1)
                .replace(' ', '')
                .replace('.', ' ')
            )
            name = cleaned_initials + ' ' + initials_matched.group(2)

        log.debug('Artist name after cleanup: %s', name)
        return name

    def find_non_contributor(self, author_array):
        """
        Find the first author in the list that is not a contributor.

        Contributors have suffixes like " - Narrator" or " - Author".
        This method finds the primary author without such tags.

        Args:
            author_array (list): List of author name strings

        Side Effects:
            Sets self.media.artist to the first non-contributor author,
            or the first author (cleaned) if all are contributors.
        """
        # Go through list of artists until we find a non contributor
        for i, r in enumerate(author_array):
            if self.clear_contributor_text(r) != r:
                log.debug('Author #%d is a contributor', i + 1)
                # If all authors are contributors use the first
                if i == len(author_array) - 1:
                    log.debug(
                        'All authors are contributors, using the first one'
                    )
                    self.media.artist = self.clear_contributor_text(
                        author_array[0]
                    )
                    return
                continue
            log.info(
                'Merging multi-author "%s" into top-level author "%s"',
                self.media.artist,
                r
            )
            self.media.artist = r
            return

    def handle_multi_artist(self):
        """
            Handles multi-artist lists.
        """
        author_array = self.media.artist.split(', ')
        if len(author_array) > 1:
            self.find_non_contributor(author_array)
        else:
            if (
                self.clear_contributor_text(self.media.artist)
                !=
                self.media.artist
            ):
                log.debug('Stripped contributor tag from author')
                self.media.artist = self.clear_contributor_text(
                    self.media.artist
                )

    def get_primary_author(self):
        """
            Checks for combined authors
            If matched, author name is set to None to prevent
            it being used in search query.
        """
        self.set_media_artist()

        # We need an author name to continue
        if not self.media.artist:
            return

        # Handle multi-artist
        self.handle_multi_artist()

    def parse_api_response(self, api_response):
        """
        Parse API response and extract relevant fields for Plex search results.

        Extracts author/artist information from Audnexus API response.

        Args:
            api_response (list): List of author dictionaries from API.

        Returns:
            list: List of result dictionaries containing:
                - asin: Author's ASIN identifier
                - name: Author's name

        Note:
            Uses dict.viewkeys() which is Python 2.7 specific.
            For Python 3 migration, replace with dict.keys() or set(dict.keys()).
        """
        search_results = []
        for item in api_response:
            # Only append results which have valid keys
            # NOTE: viewkeys() is Python 2.7 specific - use keys() for Python 3
            if item.viewkeys() >= {
                "asin",
                "name",
            }:
                search_results.append(
                    {
                        'asin': item['asin'],
                        'name': item['name'],
                    }
                )
        return search_results

    def set_media_artist(self):
        """
            Sometimes artist isn't set but title is.
        """
        if self.media.title:
            self.media.artist = self.media.title
        else:
            log.error("No artist to validate")


class ScoreTool(object):
    """
    Scoring tool for ranking search results using Levenshtein distance.

    Calculates a match score for each search result by comparing:
    - Album/book title similarity (weight: x2)
    - Author name similarity (weight: x10)
    - Language match (penalty: 2 points if mismatch)
    - Year/date proximity (penalty: up to 10 points based on year difference)
    - Edition match (bonus/penalty based on matching edition keywords)

    The initial score starts at SearchConfig.INITIAL_SCORE and deductions are made
    based on mismatches. Results scoring below SearchConfig.IGNORE_SCORE are filtered out.

    Uses SearchConfig class for all scoring thresholds and weights.

    Attributes:
        INITIAL_SCORE (int): Starting score before deductions (from SearchConfig)
        IGNORE_SCORE (int): Minimum score to include in results (from SearchConfig)
        calculate_score: Levenshtein distance function
        helper: Reference to the SearchTool instance for media access
        year (str): Year from media metadata for date matching
    """

    # Use centralized configuration constants
    INITIAL_SCORE = SearchConfig.INITIAL_SCORE
    IGNORE_SCORE = SearchConfig.IGNORE_SCORE

    def __init__(
        self,
        helper,
        index,
        info,
        locale,
        levenshtein_distance,
        result_dict,
        year=None
    ):
        """
        Initialize the ScoreTool.

        Args:
            helper: SearchTool instance providing media access and context
            index (int): Result index for relevance weighting
            info (list): List to append scored results to
            locale (str): English locale code for language comparison
            levenshtein_distance: Function for calculating string distance
            result_dict (dict): API result dictionary to score
            year (str, optional): Year from media metadata for date matching
        """
        self.calculate_score = levenshtein_distance
        self.helper = helper
        self.index = index
        self.info = info
        self.english_locale = locale
        self.result_dict = result_dict
        self.year = year

    def reduce_string(self, string):
        """
        Normalize a string for comparison by removing punctuation and case.

        Args:
            string (str): Input string to normalize

        Returns:
            str: Lowercase string with punctuation and spaces removed
        """
        normalized = string \
            .lower() \
            .replace('-', '') \
            .replace(' ', '') \
            .replace('.', '') \
            .replace(',', '')
        return normalized

    def run_score_author(self):
        """
        Prepare and score an author search result.

        Sets up instance variables from the author result dictionary
        and delegates to score_result() for scoring.

        Returns:
            None: Results are appended to self.info if score is acceptable
        """
        self.asin = self.result_dict['asin']
        self.author = self.result_dict['name']
        self.authors_concat = self.author
        self.date = None
        self.language = None
        self.narrator = None
        self.region = None
        self.title = None
        return self.score_result()

    def run_score_book(self):
        """
            Scores a book result.
        """
        self.asin = self.result_dict['asin']
        self.authors_concat = ', '.join(
            author['name'] for author in self.result_dict['author']
        )
        self.author = self.result_dict['author'][0]['name']
        self.date = self.result_dict['date']
        self.language = self.result_dict['language'].title()
        self.narrator = self.result_dict['narrator'][0]['name']
        self.region = self.result_dict['region']
        self.title = self.result_dict['title']
        return self.score_result()

    def sum_scores(self, numberlist):
        """
            Sums a list of numbers.
        """
        # Because builtin sum() isn't available
        return reduce(
            lambda x, y: x + y, numberlist, 0
        )

    def score_create_result(self, score):
        """
            Creates a result dict for the score.
            Logs the score and the data used to calculate it.
        """
        data_to_log = []
        plex_score_dict = {}

        # Go through all the keys for the result and log as we go
        if self.asin:
            plex_score_dict['id'] = self.asin
            data_to_log.append({'ASIN is': self.asin})
        if self.author:
            plex_score_dict['author'] = self.author
            data_to_log.append({'Author is': self.author})
        if self.date:
            plex_score_dict['date'] = self.date
            data_to_log.append({'Date is': self.date})
        if self.narrator:
            plex_score_dict['narrator'] = self.narrator
            data_to_log.append({'Narrator is': self.narrator})
        if self.region:
            plex_score_dict['region'] = self.region
            data_to_log.append({'Region is': self.region})
        if score:
            plex_score_dict['score'] = score
            data_to_log.append({'Score is': str(score)})
        if self.title:
            plex_score_dict['title'] = self.title
            data_to_log.append({'Title is': self.title})
        if self.year:
            plex_score_dict['year'] = self.year

        log.metadata(data_to_log, log_level="info")
        return plex_score_dict

    def score_result(self):
        """
        Calculate the overall match score for a search result.

        Combines multiple scoring factors to determine how well the API
        result matches the media file. Lower scores indicate better matches.

        Scoring factors:
            - Album title similarity (via score_album)
            - Author name similarity (via score_author)
            - Library language match (via score_language)
            - Year/date proximity (via score_year)
            - Edition match (via score_edition)

        Side Effects:
            - Appends result to self.info if score >= IGNORE_SCORE
            - Logs result information
        """
        # Array to hold score points for processing
        all_scores = []

        # Album name score
        if self.title:
            title_score = self.score_album(self.title)
            if title_score:
                all_scores.append(title_score)
        # Author name score
        if self.authors_concat:
            author_score = self.score_author(self.authors_concat)
            if author_score:
                all_scores.append(author_score)
        # Library language score
        if self.language:
            lang_score = self.score_language(self.language)
            if lang_score:
                all_scores.append(lang_score)
        # Year/date matching score
        if self.date and self.year:
            year_score = self.score_year(self.date)
            if year_score:
                all_scores.append(year_score)
        # Edition matching score
        if self.title:
            edition_score = self.score_edition(self.title)
            if edition_score:
                all_scores.append(edition_score)

        # Subtract difference from initial score
        # Subtract index to use Audible relevance as weight
        score = self.INITIAL_SCORE - self.sum_scores(all_scores) - self.index

        log.info('Result #%d', self.index + 1)

        # Create result dict
        plex_score_dict = self.score_create_result(score)

        if score >= self.IGNORE_SCORE:
            self.info.append(plex_score_dict)
        else:
            log.info(
                '# Score is below ignore boundary (%s)... Skipping!',
                self.IGNORE_SCORE
            )

    def score_year(self, result_date):
        """
        Compare the media year to the search result release date.

        Applies a score penalty based on the year difference between
        the media file's year metadata and the API result's release date.
        This helps disambiguate between different editions or re-recordings.

        Args:
            result_date (str): Release date string from API (format: YYYY-MM-DD or YYYY)

        Returns:
            int: Score deduction value (0-10, capped). Each year of difference
                 adds YEAR_PENALTY_PER_YEAR points, capped at YEAR_PENALTY_CAP.

        Example:
            >>> # Media year: 2020, Result date: 2018-03-15
            >>> self.score_year('2018-03-15')
            4  # 2 years difference * 2 = 4 points
        """
        if not self.year or not result_date:
            return 0

        try:
            # Extract year from date string (handles YYYY-MM-DD or YYYY)
            result_year = int(str(result_date)[:4])
            media_year = int(self.year)
            year_diff = abs(media_year - result_year)

            if year_diff > 0:
                # Use config constants for penalty calculation
                year_score = min(
                    year_diff * SearchConfig.YEAR_PENALTY_PER_YEAR,
                    SearchConfig.YEAR_PENALTY_CAP
                )
                log.debug('Score deduction from year mismatch (%d vs %d): %d',
                          media_year, result_year, year_score)
                return year_score
        except (ValueError, TypeError) as e:
            log.debug('Could not parse year for scoring: %s', e)

        return 0

    def score_edition(self, result_title):
        """
        Compare edition information between media and search result.

        Uses the edition metadata extracted from the media name to boost
        scores for matching editions. This helps distinguish between
        different versions of the same audiobook (e.g., "Deluxe Edition",
        "Unabridged", "Full Cast").

        The scoring uses SearchConfig constants:
        - EDITION_MATCH_BONUS: Bonus for matching edition (negative value)
        - EDITION_PARTIAL_BONUS: Bonus for partial edition match
        - EDITION_MISSING_PENALTY: Penalty if expected edition not found

        Args:
            result_title (str): The title from the API search result

        Returns:
            int: Score adjustment value. Negative = bonus, Positive = penalty.

        Example:
            >>> # Media has "deluxe edition", result title is "Dune: Deluxe Edition"
            >>> self.score_edition('Dune: Deluxe Edition')
            -5  # Bonus for matching edition
            >>> # Media has "deluxe edition", result title is "Dune"
            >>> self.score_edition('Dune')
            3  # Penalty for missing edition
        """
        if not hasattr(self.helper, 'edition') or not self.helper.edition:
            return 0

        if not result_title:
            return 0

        media_edition = self.helper.edition.lower()
        result_title_lower = result_title.lower()

        # Check if the edition keywords appear in the result title
        if media_edition in result_title_lower:
            log.debug('Edition match found: %s in result', media_edition)
            return SearchConfig.EDITION_MATCH_BONUS

        # Check for individual edition keywords
        edition_words = media_edition.split()
        for word in edition_words:
            if len(word) > 3 and word in result_title_lower:
                log.debug('Partial edition match: %s in result', word)
                return SearchConfig.EDITION_PARTIAL_BONUS

        # Edition expected but not found - small penalty
        log.debug('Edition "%s" not found in result', media_edition)
        return SearchConfig.EDITION_MISSING_PENALTY

    def score_album(self, title):
        """
        Compare the input album title similarity to the search result album title.

        Uses Levenshtein distance to calculate the similarity score between
        the media album title and the API result title. The score is weighted
        by SearchConfig.ALBUM_SCORE_WEIGHT to give album matching appropriate importance.

        Args:
            title (str): The album/book title from the API search result.

        Returns:
            int: Score deduction value. Higher values indicate less similarity.
                 Returns NO_METADATA_PENALTY if no album title found in file metadata.

        Note:
            Both strings are normalized (lowercase, punctuation removed) and
            encoded to UTF-8 before comparison to ensure consistent matching
            across different character encodings.
        """
        scorebase1 = self.helper.media.album
        if not scorebase1:
            log.error('No album title found in file metadata')
            return SearchConfig.NO_METADATA_PENALTY
        # Ensure consistent encoding for both values before comparison
        # Both must be encoded to UTF-8 to prevent comparison issues with non-ASCII chars
        if isinstance(scorebase1, unicode):
            scorebase1 = scorebase1.encode('utf-8')
        if isinstance(title, unicode):
            scorebase2 = title.encode('utf-8')
        else:
            scorebase2 = title
        album_score = self.calculate_score(
            self.reduce_string(scorebase1),
            self.reduce_string(scorebase2)
        ) * SearchConfig.ALBUM_SCORE_WEIGHT
        log.debug('Score deduction from album: %d', album_score)
        return album_score

    def score_author(self, author):
        """
        Compare the input author similarity to the search result author.

        Uses Levenshtein distance to calculate the similarity score between
        the media artist and the API result author. The score is weighted
        by SearchConfig.AUTHOR_SCORE_WEIGHT to give author matching higher importance.

        Args:
            author (str): The author name from the API search result.

        Returns:
            int: Score deduction value. Higher values indicate less similarity.
                 Returns 20 if no artist found in file metadata.
        """
        if self.helper.media.artist:
            scorebase3 = self.helper.media.artist
            scorebase4 = author
            author_score = self.calculate_score(
                self.reduce_string(scorebase3),
                self.reduce_string(scorebase4)
            ) * SearchConfig.AUTHOR_SCORE_WEIGHT
            log.debug('Score deduction from author: %d', author_score)
            return author_score

        log.warn('No artist found in file metadata')
        return 20

    def score_language(self, language):
        """
        Compare the library language to search result language.

        Deducts SearchConfig.LANGUAGE_MISMATCH_PENALTY points if the
        audiobook language doesn't match the library's configured language.

        Args:
            language (str): The language from the API search result.

        Returns:
            int: Score deduction (LANGUAGE_MISMATCH_PENALTY if mismatch, 0 if match).
        """
        lang_dict = {
            self.english_locale: 'English',
            'de': 'German',
            'es': 'Spanish',
            'fr': 'French',
            'it': 'Italian',
            'ja': 'Japanese',
        }

        if language != lang_dict[self.helper.lang]:
            log.debug(
                'Audible language: %s; Library language: %s',
                language,
                lang_dict[self.helper.lang]
            )
            log.debug("Book is not library language, deduct %d points",
                      SearchConfig.LANGUAGE_MISMATCH_PENALTY)
            return SearchConfig.LANGUAGE_MISMATCH_PENALTY
        return 0
