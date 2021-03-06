from __future__ import print_function
import sublime
import sublime_plugin

import re
import math
import time

try:
    import Default.comment as comment
except ImportError:
    import comment

from debug_tools import getLogger

from . import py_textwrap as textwrap


def is_quoted_string(scope_region, scope_name):
    # string.quoted.double.block.python
    # string.quoted.single.block.python
    # string.quoted.double.single-line.python
    # string.quoted.single.single-line.python
    # comment.block.documentation.python
    return 'quoted' in scope_name or 'comment.block.documentation' in scope_name


time_start = 0
debug_enabled = 1
log = getLogger(debug_enabled, "wrap_plus")
# log = getLogger( debug_enabled, "wrap_plus", "wrapplus.txt" )
# log = getLogger( debug_enabled, "wrap_plus", "wrapplus.txt", mode='w', time=False, msecs=False, tick=False )


def plugin_unloaded():
    # Unlocks the log file, if any
    log.delete()


def debug_start(enabled):
    global debug_enabled

    if enabled:
        debug_enabled = int(enabled) + 1
        log.debug_level = debug_enabled

    if debug_enabled > 1:
        global time_start
        time_start = time.time()


def debug_end():
    if debug_enabled > 1:
        log( 2, 'Total time %.3f', time.time() - time_start )


class PrefixStrippingView(object):
    """View that strips out prefix characters, like comments.

    :ivar str required_comment_prefix: When inside a line comment, we want to
        restrict wrapping to just the comment section, and also retain the
        comment characters and any initial indentation.  This string is the
        set of prefix characters a line must have to be a candidate for
        wrapping in that case (otherwise it is typically the empty string).

    :ivar Pattern required_comment_pattern: Regular expression required for
        matching.  The pattern is already included from the first line in
        `required_comment_prefix`.  This pattern is set to check if subsequent
        lines have longer matches (in which case `line` will stop reading).
    """
    required_comment_prefix = ''
    required_comment_pattern = None

    def __init__(self, view, min, max):
        """
            @param view the Sublime Text view from where to get the text from
            @min   the view start point to extract the text from
            @max   the view end point to extract the text from
        """
        self.view = view
        self.min = min
        self.max = max

    def _is_c_comment(self, scope_name):
        if 'comment' not in scope_name and 'block' not in scope_name:
            return False
        for start, end, disable_indent in self.block_comment:
            if start == '/*' and end == '*/':
                break
        else:
            return False
        return True

    def set_comments(self, line_comments, block_comment, point):
        self.line_comments = line_comments
        self.block_comment = block_comment

        scope_region = self.view.extract_scope(point)
        scope_name = self.view.scope_name(point)

        # If the line at point is a comment line, set required_comment_prefix to
        # the comment prefix.
        self.required_comment_prefix = ''

        # Grab the line.
        line_region = self.view.line(point)
        line = self.view.substr(line_region)
        line_stripped = line.strip()

        if not line_stripped:
            # Empty line, nothing to do.
            log(2, 'Empty line, no comment characters found.')
            return

        # Determine if point is inside a "line comment".
        # Only whitespace is allowed to the left of the line comment.
        is_generic_config = "source.genconfig" in scope_name
        log(2, "line_comments %s", line_comments)

        # When using the generic syntax, the Sublime Text plugin `Default.comment` will return
        # the default syntax comment prefix `#` instead of the actual line prefix
        if is_generic_config:
            line_comments.extend([("//", False), ("#", False), ("%", False)])

        extended_prefixes = []

        # Fix the C++/Rust extended prefix documentation styles
        # https://github.com/evandrocoan/SublimeStudio/issues/75
        for prefix, is_block_comment in line_comments:
            extended_prefixes.append( (prefix, is_block_comment) )
            extended_prefixes.append( (prefix + prefix[-1], is_block_comment) )

        for line_comment, is_block_comment in extended_prefixes:
            line_comment = line_comment.rstrip()
            log(2, "line_comment %s line_stripped %s", line_comment, line_stripped)

            if line_stripped.startswith(line_comment):

                line_difference = len(line) - len(line.lstrip())
                prefix = line[:line_difference+len(line_comment)]

                if len(self.required_comment_prefix) < len(prefix):
                    self.required_comment_prefix = prefix

        # TODO: re.escape required_comment_prefix.

        # Handle email-style quoting.
        email_quote_pattern = re.compile('^' + self.required_comment_prefix + email_quote)
        regex_match = email_quote_pattern.match(line)
        if regex_match:
            self.required_comment_prefix = regex_match.group()
            self.required_comment_pattern = email_quote_pattern
            log(2, 'doing email style quoting')

        log(2, 'scope=%r range=%r', scope_name, scope_region)

        if self._is_c_comment(scope_name):
            # Check for C-style commenting with each line starting with an asterisk.
            first_star_prefix = None
            lines = self.view.lines(scope_region)
            for line_region in lines[1:-1]:
                line = self.view.substr(line_region)
                regex_match = funny_c_comment_pattern.match(line)
                if regex_match is not None:
                    if first_star_prefix is None:
                        first_star_prefix = regex_match.group()
                else:
                    first_star_prefix = None
                    break
            if first_star_prefix:
                self.required_comment_prefix = first_star_prefix
            # Narrow the scope to just the comment contents.
            scope_text = self.view.substr(scope_region)
            regex_match = re.match(r'^([ \t\n]*/\*).*(\*/[ \t\n]*)$', scope_text, re.DOTALL)
            if regex_match:
                begin = scope_region.begin() + len(regex_match.group(1))
                end = scope_region.end() - len(regex_match.group(2))
                self.min = max(self.min, begin)
                self.max = min(self.max, end)
            log(2, 'Scope narrowed to %i:%i', self.min, self.max)

        log(2, 'required_comment_prefix determined to be %r', self.required_comment_prefix,)

        # Narrow the min/max range if inside a "quoted" string.
        if is_quoted_string(scope_region, scope_name):
            # Narrow the range.
            self.min = max(self.min, self.view.line(scope_region.begin()).begin())
            self.max = min(self.max, self.view.line(scope_region.end()).end())

    def line(self, where):
        """Get a line for a point.

        :returns: A (region, str) tuple.  str has the comment prefix stripped.
            Returns None, None if line out of range.
        """
        line_region = self.view.line(where)
        if line_region.begin() < self.min:
            log(2, 'line min increased')
            line_region = sublime.Region(self.min, line_region.end())
        if line_region.end() > self.max:
            log(2, 'line max lowered')
            line_region = sublime.Region(line_region.begin(), self.max)
        line = self.view.substr(line_region)
        log(2, 'line=%r', line)
        if self.required_comment_prefix:
            log(2, 'checking required comment prefix %r', self.required_comment_prefix)

            if line.startswith(self.required_comment_prefix):
                # Check for an insufficient prefix.
                if self.required_comment_pattern:
                    regex_match = self.required_comment_pattern.match(line)
                    if regex_match:
                        if regex_match.group() != self.required_comment_prefix:
                            # This might happen, if for example with an email
                            # comment, we go from one comment level to a
                            # deeper one (the regex matched more > characters
                            # than are in required_comment_pattern).
                            return None, None
                    else:
                        # This should never happen (matches the string but not
                        # the regex?).
                        return None, None
                rcp_len = len(self.required_comment_prefix)
                line = line[rcp_len:]
                # XXX: Should this also update line_region?
            else:
                return None, None
        return line_region, line

    def substr(self, r):
        return self.view.substr(r)

    def next_line(self, where):
        l_r = self.view.line(where)
        log(2, 'next line region=%r', l_r)
        point = l_r.end() + 1
        if point >= self.max:
            log(2, 'past max at %r', self.max)
            return None, None
        return self.line(point)

    def prev_line(self, where):
        l_r = self.view.line(where)
        point = l_r.begin() - 1
        if point <= self.min:
            return None, None
        return self.line(point)


def OR(*args):
    return '(?:' + '|'.join(args) + ')'


def CONCAT(*args):
    return '(?:' + ''.join(args) + ')'


first_group = lambda x: r"(?:^[\t \{\}\n]%s(?:````?.*)?)" % x
blank_line_pattern = re.compile( r'({}$|{}(?:[%/].*)?$|(?:.*"""\\?$)|.*\$\$.+)'.format( first_group('*'), first_group('+') ) )
next_word_pattern = re.compile(r'\s+[^ ]+', re.MULTILINE)
space_prefix_pattern = re.compile(r'^[ \t]*')
log( 4, "pattern blank_line_pattern", blank_line_pattern.pattern )

# This doesn't always work, but seems decent.
numbered_list = r'[\t ]*(?:(?:([0-9#]+)[.)])+[\t ])'
numbered_list_pattern = re.compile(numbered_list)
lettered_list = r'(?:[a-zA-Z][.)][\t ])'
bullet_list = r'(?:[*+#-]+[\t ])'
list_pattern = re.compile(r'^[ \t]*' + OR(numbered_list, lettered_list, bullet_list) + r'[ \t]*')
latex_hack = r'(?:\\)(?!,|;|&|%|text|emph|cite|\w?(page)?ref|url|footnote|(La)*TeX)'
rest_directive = r'(?:\.\.)'
field_start = r'(?:[:@])'  # rest, javadoc, jsdoc, etc.

fields = OR(r'(?<!\\):[^:]+:', '@[a-zA-Z]+ ')
field_pattern = re.compile(r'^([ \t]*)' + fields)  # rest, javadoc, jsdoc, etc
spaces_pattern = re.compile(r'^\s*$')
not_spaces_pattern = re.compile(r'[^ ]+')

sep_chars = '!@#$%^&*=+`~\'\":;.,?_-'
sep_line = r'[{sep}]+[(?: |\t){sep}]*'.format(sep=sep_chars)

# Break pattern is a little ambiguous. Something like "# Header" could also be a list element.
break_pattern = re.compile(r'^[\t ]*' + OR(sep_line, OR(latex_hack, rest_directive) + '.*') + '$')
pure_break_pattern = re.compile(r'^[\t ]*' + sep_line + '$')

email_quote = r'[\t ]*>[> \t]*'
funny_c_comment_pattern = re.compile(r'^[\t ]*\*')


class WrapLinesPlusCommand(sublime_plugin.TextCommand):

    def __init__(self, view):
        super( WrapLinesPlusCommand, self ).__init__( view )

        self.maximum_words_in_comma_separated_list = 4
        self.maximum_items_in_comma_separated_list = 4

    def _my_full_line(self, region):
        # Special case scenario where you select an entire line.  The normal
        # "full_line" function will extend it to contain the next line
        # (because the cursor is actually at the beginning of the next line).
        # I would prefer it didn't do that.
        if self.view.substr(region.end() - 1) == '\n':
            return self.view.full_line(sublime.Region(region.begin(), region.end() - 1))
        else:
            return self.view.full_line(region)

    def _is_real_numbered_list(self, line_region, line, limit=10, indent=False):
        """Returns True if `line` is not a paragraph continuation."""
        # We stop checking the list after `limit` lines to avoid quadratic
        # runtime. For inputs like 100 lines of "2. ", this function is called
        # in a loop over the input and also contains a loop over the input.
        # indent tracks whether we came from an indented line
        if limit == 0:
            log( 2, 'limit', limit )
            return True
        regex_match = numbered_list_pattern.search(line)
        if regex_match and regex_match.group(1) == '1':
            log( 2, 'regex_match %r', regex_match.group(1), '%r' % line )
            return True
        prev_line_region, prev_line = self._strip_view.prev_line(line_region)
        if prev_line_region is None:
            log( 2, 'prev_line_region', prev_line_region, 'prev_line %r' % prev_line )
            return not indent
        if self._is_paragraph_break(prev_line_region, prev_line):
            log( 2, '_is_paragraph_break', self._is_paragraph_break(prev_line_region, prev_line) )
            return not indent
        if new_paragraph_pattern.match(prev_line):
            log( 2, 'new_paragraph_pattern', new_paragraph_pattern.match(prev_line), '%r' % prev_line )
            return not indent
        if prev_line[0] == ' ' or prev_line[0] == '\t':
            log( 2, 'prev_line might be a numbered list or a normal paragraph: %r', prev_line )
            return self._is_real_numbered_list(prev_line_region, prev_line, limit - 1, indent=True)
        if numbered_list_pattern.match(prev_line):
            log( 2, 'numbered_list_pattern.match(prev_line)', numbered_list_pattern.match(prev_line).group(0), '%r' % prev_line )
            return self._is_real_numbered_list(prev_line_region, prev_line, limit - 1)
        log( 2, 'previous line appears to be a normal paragraph: %r', line )
        return False

    def _is_paragraph_start(self, line_region, line):
        # Certain patterns at the beginning of the line indicate this is the
        # beginning of a paragraph.
        if new_paragraph_pattern.match(line):
            log( 2, 'is not a new paragraph %r', line )
            return True
        if numbered_list_pattern.match(line):
            result = self._is_real_numbered_list(line_region, line)
            log( 2, 'is %sa paragraph continuation', 'not ' if result else '', '%r' % line )
            return result
        log( 2, 'is not a paragraph %r', line )
        return False

    def _is_paragraph_break(self, line_region, line, pure=False):
        """A paragraph "break" is something like a blank line, or a horizontal line,
        or anything that should not be wrapped and treated like a blank line
        (i.e. ignored).
        """
        if self._is_blank_line(line): return True
        scope_name = self.view.scope_name(line_region.begin())
        log(2, 'scope_name=%r %r line=%r', scope_name, line_region, line)

        if 'heading' in scope_name:
            log(2, "'heading' in scope_name")
            return True
        if pure:
            pure_break = pure_break_pattern.match(line) is not None
            log(2, 'pure_break', pure_break)
            return pure_break
        else:
            normal_break = break_pattern.match(line) is not None
            log(2, 'normal_break', normal_break)
            return normal_break

    def _is_blank_line(self, line):
        is_blank_line = blank_line_pattern.match(line) is not None
        log(2, is_blank_line)
        return is_blank_line

    def _find_paragraph_start(self, point):
        """Start at point and move up to find where the paragraph starts.

        :returns: The (line, line_region) of the start of the paragraph.
        """
        view = self._strip_view
        current_line_region, current_line = view.line(point)
        if current_line_region is None:
            return None, None
        started_in_comment = self._started_in_comment(point)

        log(2, 'is_paragraph_break?')
        if self._is_paragraph_break(current_line_region, current_line):
            log(2, 'yes')
            return current_line_region, current_line
        log(2, 'no')

        while 1:
            # Check if this line is the start of a paragraph.
            log(2, 'is the start of a paragraph?')
            if self._is_paragraph_start(current_line_region, current_line):
                log(2, 'yes, current_line is paragraph start %r', current_line,)
                break
            log(2, 'no')
            # Check if the previous line is a "break" separator.
            log(2, 'previous line is line break?')
            prev_line_region, prev_line = view.prev_line(current_line_region)
            if prev_line_region is None:
                log(2, "yes, current_line is as far up as we're allowed to go.")
                break
            if self._is_paragraph_break(prev_line_region, prev_line):
                log(2, 'yes, prev line %r is a paragraph break', prev_line,)
                break
            # If the previous line has a comment, and we started in a
            # non-comment scope, stop.  No need to check for comment to
            # non-comment change because the prefix restrictions should handle
            # that.
            if (not started_in_comment
                and self.view.score_selector(prev_line_region.end(), 'comment')
               ):
                log(2, 'yes, prev line %r contains a comment, cannot continue.', prev_line)
                break
            log(2, 'no, prev_line %r is part of the paragraph', prev_line,)
            # Previous line is a part of this paragraph.  Add it, and loop
            # around again.
            current_line_region = prev_line_region
            current_line = prev_line
        return current_line_region, current_line

    def _find_paragraphs(self, sublime_text_region):
        """Find and return a list of paragraphs as regions.

        :param Region sublime_text_region: The region where to look for paragraphs.  If it is
            an empty region, "discover" where the paragraph starts and ends.
            Otherwise, the region defines the max and min (with potentially
            several paragraphs contained within).

        :returns: A list of (region, lines, comment_prefix) of each paragraph.
        """
        result = []
        log(2, 'sublime_text_region=%r', sublime_text_region,)
        if sublime_text_region.empty():
            is_empty = True
            view_min = 0
            view_max = self.view.size()
        else:
            is_empty = False
            full_sr = self._my_full_line(sublime_text_region)
            view_min = full_sr.begin()
            view_max = full_sr.end()
        started_in_comment = self._started_in_comment(sublime_text_region.begin())
        self._strip_view = PrefixStrippingView(self.view, view_min, view_max)
        view = self._strip_view
        # Loop for each paragraph (only loops once if sublime_text_region is empty).
        paragraph_start_pt = sublime_text_region.begin()
        first_selection_to_save = paragraph_start_pt
        while 1:
            log(2, 'paragraph scanning start %r.', paragraph_start_pt,)
            view.set_comments(self._line_comment, self._is_block_comment, paragraph_start_pt)
            lines = []
            if is_empty:
                # Find the beginning of this paragraph.
                log(2, 'empty sel finding paragraph start.')
                current_line_region, current_line = self._find_paragraph_start(paragraph_start_pt)
                log(2, 'empty sel paragraph start determined to be %r %r',
                      current_line_region, current_line)
            else:
                # The selection defines the beginning.
                current_line_region, current_line = view.line(paragraph_start_pt)
                log(2, 'sel beginning = %r %r', current_line_region, current_line)

            if current_line_region is None:
                log(2, 'Could not find start.')
                return []

            # Skip blank and unambiguous break lines.
            while 1:
                log(2, 'skip blank line?')
                if not self._is_paragraph_break(current_line_region, current_line, pure=True):
                    log(2, 'yes, not paragraph break')
                    break
                if is_empty:
                    log(2, 'empty sel on paragraph break %r', current_line,)
                    return []
                new_current_line_region, new_current_line = view.next_line(current_line_region)
                log( 2, 'current_line_region', new_current_line_region, 'current_line', new_current_line )
                if new_current_line is None: break
                current_line_region, current_line = new_current_line_region, new_current_line

            paragraph_start_pt = current_line_region.begin()
            paragraph_end_pt = current_line_region.end()
            # current_line_region now points to the beginning of the paragraph.
            # Move down until the end of the paragraph.
            log(2, 'Scan until end of paragraph.')
            while 1:
                log(2, 'current_line_region=%r max=%r', current_line_region, view.max)
                # If we started in a non-comment scope, and the end of the
                # line contains a comment, include any non-comment text in the
                # wrap and stop looking for more.
                if (not started_in_comment
                    and self.view.score_selector(current_line_region.end(), 'comment')
                   ):
                    log(2, 'end of paragraph hit a comment.')
                    # Find the start of the comment.
                    # This assumes comments do not have multiple scopes.
                    comment_r = self.view.extract_scope(current_line_region.end())
                    # Just in case something is wonky with the scope.
                    end_pt = max(comment_r.begin(), current_line_region.begin())
                    # A substring of current_line.
                    sublime_region = sublime.Region(current_line_region.begin(), end_pt)
                    region_substring = self.view.substr(sublime_region)
                    # Do not include whitespace preceding the comment.
                    regex_match = re.search('([ \t]+$)', region_substring)
                    if regex_match:
                        end_pt -= len(regex_match.group(1))
                    log(2, 'non-comment contents are %r', region_substring)
                    paragraph_end_pt = end_pt
                    lines.append(region_substring)
                    # Skip over the comment.
                    current_line_region, current_line = view.next_line(current_line_region)
                    break

                lines.append(current_line)
                paragraph_end_pt = current_line_region.end()

                current_line_region, current_line = view.next_line(current_line_region)
                if current_line_region is None:
                    # Line is outside of our range.
                    log(2, 'Out of range, stopping.')
                    break
                log(2, 'current_line = %r %r', current_line_region, current_line)
                if self._is_paragraph_break(current_line_region, current_line):
                    log(2, 'current line is a break, stopping.')
                    break
                if self._is_paragraph_start(current_line_region, current_line):
                    log(2, 'current line is a paragraph start, stopping.')
                    break

            paragraph_region = sublime.Region(paragraph_start_pt, paragraph_end_pt)

            if first_selection_to_save:
                result.append((paragraph_region, lines, view.required_comment_prefix, first_selection_to_save))
                first_selection_to_save = None
            else:
                result.append((paragraph_region, lines, view.required_comment_prefix, paragraph_start_pt))

            if is_empty:
                break

            # Skip over blank lines and break lines till the next paragraph
            # (or end of range).
            log(2, 'skip over blank lines')
            while current_line_region is not None:
                if self._is_paragraph_start(current_line_region, current_line):
                    break
                if not self._is_paragraph_break(current_line_region, current_line):
                    break
                # It's a paragraph break, skip over it.
                current_line_region, current_line = view.next_line(current_line_region)

            if current_line_region is None:
                break

            log(2, 'next_paragraph_start is %r %r', current_line_region, current_line)
            paragraph_start_pt = current_line_region.begin()
            if paragraph_start_pt >= view_max:
                break

        return result

    def _determine_width(self, width):
        """Determine the maximum line width.

        :param Int width: The width specified by the command.  Normally 0
            which means "figure it out".

        :returns: The maximum line width.
        """
        log( 4, "width %s", width )
        if width == 0 and self.view.settings().get('wrap_width'):
            try:
                width = int(self.view.settings().get('wrap_width'))
            except TypeError:
                pass

        log( 4, "before get('rulers') width %s", width )
        if width == 0 and self.view.settings().get('rulers'):
            # try and guess the wrap width from the ruler, if any
            try:
                width = int(self.view.settings().get('rulers')[0])
            except ValueError:
                pass
            except TypeError:
                pass

        log( 4, "before get('WrapPlus.wrap_width') %s", width )
        if width == 0:
            width = self.view.settings().get('WrapPlus.wrap_width', width)

        # Value of 0 means 'automatic'.
        if width == 0:
            width = 78

        ile = self.view.settings().get('WrapPlus.include_line_endings', 'auto')
        if ile is True:
            width -= self._determine_line_ending_size()
        elif ile == 'auto':
            if self._auto_word_wrap_enabled() and self.view.settings().get('wrap_width', 0) != 0:
                width -= self._determine_line_ending_size()

        return width

    def _determine_line_ending_size(self):
        # Sublime always uses 1, regardless of the file type/OS.
        return 1
        etypes = {
            'windows': 2,
            'unix': 1,
            'cr': 1,
        }
        return etypes.get(self.view.line_endings().lower(), 1)

    def _auto_word_wrap_enabled(self):
        ww = self.view.settings().get('word_wrap')
        return (ww is True or
                (ww == 'auto' and self.view.score_selector(0, 'text')))

    def _determine_tab_size(self):
        tab_width = 8
        if self.view.settings().get('tab_size'):
            try:
                tab_width = int(self.view.settings().get('tab_size'))
            except TypeError:
                pass

        if tab_width == 0:
            tab_width = 8
        self._tab_width = tab_width

    def _determine_comment_style(self):
        # I'm not exactly sure why this function needs a point.  It seems to
        # return the same value regardless of location for the stuff I've
        # tried.
        (self._line_comment, self._is_block_comment) = comment.build_comment_data(self.view, 0)

    def _started_in_comment(self, point):
        if self.view.score_selector(point, 'comment'):
            return True
        # Check for case where only whitespace is before a comment.
        line_region = self.view.line(point)
        if self.view.score_selector(line_region.end(), 'comment'):
            line = self.view.substr(line_region)
            regex_match = re.search('(^[ \t]+)', line)
            if regex_match:
                pt_past_space = line_region.begin() + len(regex_match.group(1))
                if self.view.score_selector(pt_past_space, 'comment'):
                    return True
        return False

    def _width_in_spaces(self, text):
        tab_count = text.count('\t')
        return tab_count * self._tab_width + len(text) - tab_count

    def _make_indent(self):
        # This is suboptimal.
        return ' ' * 4
        # if self.view.settings().get('translate_tabs_to_spaces'):
        #     return ' ' * self._tab_width
        # else:
        #     return '\t'

    def _extract_prefix(self, paragraph_region, lines, required_comment_prefix):
        # The comment prefix has already been stripped from the lines.
        # If the first line starts with a list-like thing, then that will be the initial prefix.
        initial_indent = ''
        subsequent_indent = ''
        first_line = lines[0]
        regex_match = list_pattern.match(first_line)
        if regex_match:
            initial_indent = first_line[0:regex_match.end()]
            log(2, 'setting initial_indent', initial_indent)

            stripped_prefix = initial_indent.lstrip()
            leading_whitespace = initial_indent[:len(initial_indent) - len(stripped_prefix)]
            subsequent_indent = leading_whitespace + ' ' * self._width_in_spaces(stripped_prefix)
        else:
            regex_match = field_pattern.match(first_line)
            if regex_match:
                # The spaces in front of the field start.
                initial_indent = regex_match.group(1)
                log(2, 'setting initial_indent', initial_indent)
                if len(lines) > 1:
                    # How to handle subsequent lines.
                    regex_match = space_prefix_pattern.match(lines[1])
                    if regex_match:
                        # It's already indented, keep this indent level
                        # (unless it is less than where the field started).
                        spaces = regex_match.group(0)
                        if (self._width_in_spaces(spaces) >=
                            self._width_in_spaces(initial_indent) + 1
                           ):
                            subsequent_indent = spaces
                if not subsequent_indent:
                    # Not already indented, make an indent.
                    subsequent_indent = initial_indent + self._make_indent()
            else:
                regex_match = space_prefix_pattern.match(first_line)
                if regex_match:
                    initial_indent = first_line[0:regex_match.end()]
                    if len(lines) > 1:
                        regex_match = space_prefix_pattern.match(lines[1])
                        if regex_match:
                            subsequent_indent = lines[1][0:regex_match.end()]
                        else:
                            subsequent_indent = ''
                    else:
                        subsequent_indent = initial_indent
                else:
                    # Should never happen.
                    initial_indent = ''
                    subsequent_indent = ''

        point = paragraph_region.begin()
        scope_region = self.view.extract_scope(point)
        scope_name = self.view.scope_name(point)
        if len(lines) == 1 and is_quoted_string(scope_region, scope_name):
            # A multi-line quoted string, that is currently only on one line.
            # This is mainly for Python docstrings.  Not sure if it's a
            # problem in other cases.
            true_first_line_r = self.view.line(point)
            true_first_line = self.view.substr(true_first_line_r)
            if true_first_line_r.begin() <= scope_region.begin():
                regex_match = space_prefix_pattern.match(true_first_line)
                log(2, 'single line quoted string triggered')
                if regex_match:
                    subsequent_indent = regex_match.group() + subsequent_indent

        # Remove the prefixes that are there.
        new_lines = []
        new_lines.append(first_line[len(initial_indent):].strip())
        for line in lines[1:]:
            if line.startswith(subsequent_indent):
                line = line[len(subsequent_indent):]
            new_lines.append(line.strip())

        log(2, 'initial_indent=%r subsequent_indent=%r', initial_indent, subsequent_indent)

        return (required_comment_prefix + initial_indent,
                required_comment_prefix + subsequent_indent,
                new_lines)

    def get_semantic_line_wrap_setting(self, line_wrap_type):
        is_semantic_line_wrap = self.view_settings.get( 'WrapPlus.semantic_line_wrap', False )

        if line_wrap_type:

            if line_wrap_type == "semantic":
                is_semantic_line_wrap = True

            if line_wrap_type == "classic":
                is_semantic_line_wrap = False

        return is_semantic_line_wrap

    def run(self, edit, width=0, line_wrap_type=None):
        debug_enabled = self.view.settings().get('WrapPlus.debug', False)
        debug_start(debug_enabled)
        log(2, '\n\n#########################################################################')

        self._width = self._determine_width(width)
        self.view_settings = self.view.settings()

        log(4,'wrap width = %r', self._width)
        self._determine_tab_size()
        self._determine_comment_style()

        wrap_extension_percent                 = self.view_settings.get('WrapPlus.semantic_wrap_extension_percent', 1.0)
        minimum_line_size_percent              = self.view_settings.get('WrapPlus.semantic_minimum_line_size_percent', 0.2)
        balance_characters_between_line_wraps  = self.view_settings.get('WrapPlus.semantic_balance_characters_between_line_wraps', False)
        disable_line_wrapping_by_maximum_width = self.view_settings.get('WrapPlus.semantic_disable_line_wrapping_by_maximum_width', False)

        global whitespace_character
        global alpha_separator_characters
        global list_separator_characters
        global word_separator_characters
        global phrase_separator_characters

        whitespace_character = self.view_settings.get( 'WrapPlus.whitespace_character', [" ", "\t"] )
        alpha_separator_characters = self.view_settings.get( 'WrapPlus.alpha_separator_characters', ['e', 'and'] )
        list_separator_characters = self.view_settings.get( 'WrapPlus.list_separator_characters', [ ",", ";"] )
        word_separator_characters = self.view_settings.get( 'WrapPlus.word_separator_characters', [ ".", "?", "!", ":" ] )
        word_separator_characters += list_separator_characters
        phrase_separator_characters = set( word_separator_characters ) - set( list_separator_characters )

        global start_line_block
        global new_paragraph_pattern

        start_line_block = self.view_settings.get( 'WrapPlus.start_line_block', r'(?:\{|\})' )
        new_paragraph_pattern = re.compile( r'^[\t ]*' + OR( lettered_list, bullet_list, field_start, start_line_block ) )
        log( 4, "pattern new_paragraph", new_paragraph_pattern.pattern )

        after_wrap = self.view_settings.get('WrapPlus.after_wrap', "cursor_below")
        self.maximum_words_in_comma_separated_list = self.view_settings.get('WrapPlus.semantic_maximum_words_in_comma_separated_list', 3) + 1
        self.maximum_items_in_comma_separated_list = self.view_settings.get('WrapPlus.semantic_maximum_items_in_comma_separated_list', 3) + 1

        if balance_characters_between_line_wraps:
            # minimum_line_size_percent = 0.0
            disable_line_wrapping_by_maximum_width = True

        log( 4, "minimum_line_size_percent %s", minimum_line_size_percent )
        if self.get_semantic_line_wrap_setting(line_wrap_type ):
            self._width *= wrap_extension_percent

            def line_wrapper_type(paragraph_lines, initial_indent, subsequent_indent, wrapper):
                text = self.semantic_line_wrap( paragraph_lines, initial_indent, subsequent_indent,
                        minimum_line_size_percent, disable_line_wrapping_by_maximum_width,
                        balance_characters_between_line_wraps )

                if balance_characters_between_line_wraps:
                    text = self.balance_characters_between_line_wraps( wrapper, text, initial_indent, subsequent_indent )

                log( 4, 'run, text %r', "".join( text ) )
                return "".join( text )

        else:
            def line_wrapper_type(paragraph_lines, initial_indent, subsequent_indent, wrapper):
                return self.classic_wrap_text(wrapper, paragraph_lines, initial_indent, subsequent_indent)

        # paragraphs is a list of (region, lines, comment_prefix) tuples.
        paragraphs = []
        has_trailing_whitespace = False
        selections = self.view.sel()

        if selections:
            possible_last_region = self.view.word(selections[0])
            possible_last_space = self.view.substr(possible_last_region)
            has_trailing_whitespace = possible_last_space \
                    and not not spaces_pattern.match(possible_last_space) \
                    and possible_last_space[-1] == '\n'
            log(2, 'possible_last_space %r' % possible_last_space, 'has_trailing_whitespace', has_trailing_whitespace)

            for selection in selections:
                log(2, 'examine %r', selection)
                paragraphs.extend(self._find_paragraphs(selection))

        log( 2, 'paragraphs is %r', paragraphs )
        log( 4, "self._width %s", self._width )

        if paragraphs:
            new_positions = self.insert_wrapped_text(edit, paragraphs, line_wrapper_type)

            if after_wrap == "cursor_below":
                self.move_cursor_below_the_last_paragraph()

            if new_positions:
                if after_wrap == "cursor_stay":
                    self.move_the_cursor_to_the_original_position( new_positions )

                if has_trailing_whitespace:
                    last_position = new_positions[-1]
                    self.view.insert( edit, last_position, " " )
        else:
            if after_wrap == "cursor_below":
                self.move_cursor_below_the_last_paragraph()

    def insert_wrapped_text(self, edit, paragraphs, line_wrapper_type):
        new_positions = []
        break_long_words = self.view_settings.get('WrapPlus.break_long_words', False)
        break_on_hyphens = self.view_settings.get('WrapPlus.break_on_hyphens', False)

        # Use view selections to handle shifts from the replace() command.
        self.view.sel().clear()
        for index, others in enumerate(paragraphs):
            region, lines, comment_prefix, cursor_position = others
            self.view.sel().add(region)

        # Regions fetched from view.sel() will shift appropriately with
        # the calls to replace().
        for index, selection in enumerate(self.view.sel()):
            paragraph_region, paragraph_lines, required_comment_prefix, cursor_position = paragraphs[index]

            wrapper = textwrap.TextWrapper(break_long_words=break_long_words, break_on_hyphens=break_on_hyphens)
            wrapper.width = self._width
            wrapper.expand_tabs = False

            initial_indent, subsequent_indent, paragraph_lines = self._extract_prefix(
                paragraph_region, paragraph_lines, required_comment_prefix)

            wrapped_text = line_wrapper_type(paragraph_lines, initial_indent, subsequent_indent, wrapper)
            original_text = self.view.substr(selection)
            log(2, 'wrapped_text len', len(wrapped_text))
            log(2, 'original_text len', len(original_text))

            if original_text != wrapped_text:

                while True:
                    word_region = self.view.word(cursor_position)
                    actual_word = self.view.substr(word_region).strip(' ')
                    log(2, 'cursor_position', cursor_position)
                    log(2, 'actual_word %r' % actual_word)

                    if cursor_position < 1 or not spaces_pattern.match(actual_word):
                        break
                    cursor_position -= 1

                cut_original_text = self.view.substr( sublime.Region( selection.begin(), word_region.end() ) )
                distance_word_end = cursor_position - word_region.begin()
                log(2, 'distance_word_end', distance_word_end)

                wrapped_text_difference = abs( len(original_text.rstrip(' ')) - len(wrapped_text) ) + 1
                log(2, 'wrapped_text_difference', wrapped_text_difference)

                self.view.replace(edit, selection, wrapped_text)
                replaced_region = sublime.Region( selection.begin(), word_region.end() + wrapped_text_difference )
                cut_replaced_text = self.view.substr( replaced_region )
                last_position = cut_replaced_text.rfind( actual_word )
                log(2, 'last_position', last_position)

                if last_position > -1:
                    actual_position = selection.begin() + last_position + distance_word_end
                    log(2, 'new actual_position', actual_position)
                    new_positions.append( actual_position )

                else:
                    # fallback to the original heuristic if the word is not found
                    spaces_count_original = len( [char for char in cut_original_text if spaces_pattern.match(char)] )
                    spaces_count_wrapped = len( [char for char in cut_replaced_text if spaces_pattern.match(char)] )
                    log(2, 'spaces_count_original', spaces_count_original)
                    log(2, 'spaces_count_wrapped', spaces_count_wrapped)

                    added_spaces_count = cursor_position + spaces_count_wrapped - spaces_count_original
                    log(2, 'new actual_position', added_spaces_count)
                    new_positions.append(added_spaces_count)

                log(2, 'cut_original_text %r' % cut_original_text)
                log(2, 'cut_replaced_text %r' % cut_replaced_text)
                log(2, 'replaced text not the same!')

            else:
                new_positions.append(cursor_position)
                log(2, 'replaced text is the same')

        return new_positions

    def move_the_cursor_to_the_original_position(self, new_positions):
        self.view.sel().clear()

        for index, position in enumerate(new_positions):
            self.view.sel().add( sublime.Region( position, position ) )

    def move_cursor_below_the_last_paragraph(self):
        selection = self.view.sel()
        end = selection[len(selection) - 1].end()
        line = self.view.line(end)
        end = min(self.view.size(), line.end() + 1)
        self.view.sel().clear()
        region = sublime.Region(end)
        self.view.sel().add(region)
        self.view.show(region)
        debug_end()

    def balance_characters_between_line_wraps(self, wrapper, text_lines, initial_indent, subsequent_indent):
        """
            input:  ['This is my very long line which will wrap near its end,']
            output: ['    ', 'This is my very long line which\n    ', 'will wrap near its end,']
        """
        wrapper.width             = self._width
        wrapper.initial_indent    = ""
        wrapper.subsequent_indent = subsequent_indent
        subsequent_indent_length  = len( subsequent_indent )
        log( 4, 'text_lines', text_lines )

        # `decrement_percent` must be stronger than 1.1, i.e., 1.1*1.1 = 1.21*0.9 = 1.089 < 1.1
        # otherwise this could immediately fail as the last line length would already be
        # greater than `self._width / 2`
        INCREMENT_VALUE = 1.05
        DECREMENT_VALUE = 0.95

        new_text          = []
        splited_lines     = self._split_lines( wrapper, text_lines, self._width )

        for index, new_lines in enumerate( splited_lines ):
            lines_count = len( new_lines )

            if lines_count > 1:
                increment_percent  = INCREMENT_VALUE
                new_lines_reversed = list( reversed( new_lines ) )

                # When there are more than 1 lines, we can get a situation like this:
                # new_lines: ['    This is my very long line\n    which will wrap near its\n    end,']
                for _index, new_line in enumerate( new_lines_reversed ):
                    next_index = _index + 1

                    if next_index < lines_count \
                            and len( new_line ) - subsequent_indent_length \
                            < math.ceil( ( len( new_lines_reversed[next_index] ) - subsequent_indent_length ) / 2 ):

                        increment_percent = INCREMENT_VALUE
                        first_lines_count = lines_count

                        # Try to increase the maximum width until the trailing line vanishes
                        while lines_count == first_lines_count \
                                and increment_percent < 2:

                            new_lines = self._split_lines( wrapper, [text_lines[index]], self._width, increment_percent )[0]

                            first_lines_count  = len( new_lines )
                            increment_percent *= INCREMENT_VALUE

                        break

                log.clean( 4, "" )
                log( 4, "Shrinking the lines... '%s'", new_lines )
                new_lines_backup = list( new_lines )

                if self.is_there_line_over_the_wrap_limit( new_lines ):
                    decrement_percent = increment_percent * DECREMENT_VALUE
                    new_lines = self._split_lines( wrapper, [text_lines[index]], self._width, decrement_percent )[0]

                    # Try to decrease the maximum width until create a trailing new line
                    while ( self.is_there_line_over_the_wrap_limit( new_lines ) \
                            or self.is_line_bellow_half_wrap_limit( new_lines, subsequent_indent_length ) ) \
                                and decrement_percent > 0.4:

                        decrement_percent *= DECREMENT_VALUE
                        new_lines = self._split_lines( wrapper, [text_lines[index]], self._width, decrement_percent )[0]

                # If still there are lines over the limit, it means some line has some very big word
                # or some very big indentation, then there is nothing we can do other than discard
                # the results. Comment this out, and you will see the Unit Tests failing with it.
                lonely_word_line = self.is_there_lonely_word_line( new_lines )

                if lonely_word_line:
                    new_lines = self._split_lines( wrapper, [text_lines[index]], self._width, lonely_word_line )[0]

                elif self.is_there_line_over_the_wrap_limit( new_lines ):
                    new_lines = new_lines_backup

            if index < 1:
                new_text.append( initial_indent )
                new_text.extend( new_lines )

            else:
                new_text.append( subsequent_indent )
                new_text.extend( new_lines )

        log( 4, "new_text %s", new_text )
        return new_text

    def is_line_bellow_half_wrap_limit(self, new_lines, subsequent_indent_length):
        return len( new_lines[-1] ) - subsequent_indent_length \
            < math.floor( ( self._width - subsequent_indent_length ) / 1.8 )

    def is_there_line_over_the_wrap_limit(self, new_lines):
        """
            We need to check whether some line has passed over the wrap limit. This can happen
            when a line with width 160 can be split in 2 lines of width 80, but not all the
            words fit on the first line with 80 characters exactly.
        """
        for new_line in new_lines:

            if len( new_line ) > self._width:
                return True

        return False

    def is_there_lonely_word_line(self, new_lines, maximumwidth=None, limitpercent=None):
        """
            Check whether there is some line with a single big word only.

            If so, it means, we must to stop wrapping with traditional line balancing algorithm.
        """
        limitpercent = limitpercent if limitpercent else 0.8
        maximumwidth = maximumwidth if maximumwidth else self._width

        for new_line in new_lines:
            longest = -1
            line_length = len( new_line )
            line_percent_size = math.ceil( line_length / maximumwidth )

            for match in not_spaces_pattern.finditer( new_line ):
                start, end = match.span()
                length = end - start

                if length > longest:
                    longest = length

            percentwidth = 0.95 if longest > maximumwidth else line_percent_size
            line_limit = maximumwidth * limitpercent
            log( 4, 'line_percent_size', line_percent_size, 'line_length', line_length,
                    'longest', longest, 'percentwidth', percentwidth, 'line_limit', line_limit,
                    'new_line', new_line )

            if longest > line_limit:
                log( 4, 'TRUE, percentwidth', percentwidth )
                return percentwidth

        log( 4, 'FALSE' )
        return False

    def is_there_big_word_on_line(self, line, new_width):
        """
            Check whether there is some big word on the line.

            If so, returns the `new_width` properly fixed for wrapping.fill()
        """
        longest = -1
        wordlimit = new_width * 0.5

        for match in not_spaces_pattern.finditer( line ):
            start, end = match.span()
            length = end - start

            if length > longest:
                longest = length

        log( 4, 'longest', longest, 'wordlimit', wordlimit, 'new_width', new_width )
        if longest > wordlimit:
            new_width = new_width + wordlimit * 0.1
            log( 4, 'new_width', new_width )
            return new_width

        return new_width

    def _split_lines(self, wrapper, text_lines, maximum_line_width, middle_of_the_line_increment_percent=1):
        """
            (input)  text_lines: ['    This is my very long line which will wrap near its end,\n']
            (output) new_lines:  [['    This is my very long line\n', '    which will wrap near its\n', '    end,\n']]
        """
        new_lines = []
        log( 4, 'text_lines', text_lines )

        initial_indent    = wrapper.initial_indent
        subsequent_indent = wrapper.subsequent_indent

        for line in text_lines:
            lines_count, line_length = self.calculate_lines_count(line, initial_indent, subsequent_indent, maximum_line_width)

            for step in range( 1, lines_count + 1 ):
                new_line_length = math.ceil( line_length / step )
                log( 4, "new_line_length %d lines_count %d", new_line_length, lines_count )

                if new_line_length > maximum_line_width:
                    continue

                else:
                    break

            new_width = math.ceil( new_line_length * middle_of_the_line_increment_percent )
            log( 4, "maximum_line_width %d new_width %d (%f)", maximum_line_width, new_width, middle_of_the_line_increment_percent )

            log( 4, "line %r", line )
            wrapper.width = self.is_there_big_word_on_line( line, new_width )
            wrapped_line  = wrapper.fill( line )

            log( 4, "wrapped_line %r", wrapped_line )
            wrapped_lines = wrapped_line.split( "\n" )

            # Add again the removed `\n` character due the `split` statement
            fixed_wrapped_lines = []

            for _wrapped_line in wrapped_lines:
                fixed_wrapped_lines.append( _wrapped_line + "\n" )

            # The last line need to be manually fixed by removing the trailing last time, if not existent on the original
            if line[-1] != "\n":
                fixed_wrapped_lines[-1] = fixed_wrapped_lines[-1][0:-1]

            new_lines.append( fixed_wrapped_lines )

        log.clean(4, "")
        log( 4, "new_lines %s", new_lines )
        return new_lines

    def calculate_lines_count(self, line, initial_indent, subsequent_indent, maximum_line_width):
        """
            We do not know how many lines there will be directly because when the wrap lines,
            the total `line_length` is increase by the `subsequent_indent`.
        """
        initial_indent_length    = len( initial_indent )
        subsequent_indent_length = len( subsequent_indent )

        lines_count = 0
        line_length = len( line ) + initial_indent_length

        new_line_length  = len( line ) + initial_indent_length
        last_line_length = 0

        while last_line_length != new_line_length \
                and lines_count < line_length:

            log( 4, "new_line_length %s", new_line_length )
            last_line_length = new_line_length

            lines_count     = math.ceil( last_line_length / maximum_line_width )
            new_line_length = ( lines_count - 1 ) * subsequent_indent_length + line_length

        log( 4, "lines_count     %s", lines_count )
        return lines_count, new_line_length

    def semantic_line_wrap(self, paragraph_lines, initial_indent="", subsequent_indent="",
                minimum_line_size_percent=0.0, disable_line_wrapping_by_maximum_width=False,
                balance_characters_between_line_wraps=False):
        """
            input: ['This is my very long line which will wrap near its', 'end,']
            if balance_characters_between_line_wraps:
                output: ['This is my very long line which will wrap near its end,']
            else:
                output: ['    ', 'This is my very long line which will wrap near its\n    ', 'end,']
        """
        new_text = []

        initial_indent_length    = len( initial_indent )
        subsequent_indent_length = len( subsequent_indent )

        if not balance_characters_between_line_wraps:
            new_text.append( initial_indent )

        is_allowed_to_wrap           = False
        is_possible_space            = False
        is_flushing_comma_list       = False
        is_comma_separated_list      = False
        is_flushing_accumalated_line = False

        text        = ' '.join(paragraph_lines)
        text_length = len(text)

        minimum_line_size = int( self._width * minimum_line_size_percent )
        log( 4, "minimum_line_size %s", minimum_line_size )

        indent_length        = initial_indent_length
        accumulated_line     = ""
        line_start_index     = 0
        comma_list_size      = 0
        last_comma_list_size = 0

        def force_flush_accumulated_line():
            nonlocal index
            nonlocal is_flushing_accumalated_line

            log( 4, "Flushing accumulated_line... next_word_length %d", next_word_length )
            is_flushing_accumalated_line = True

            # Current character is a whitespace, but it must the the next, so fix the index
            index -= 1

        for index, character in enumerate( text ):
            accumulated_line_length = len( accumulated_line )
            next_word_length        = self.peek_next_word_length( index, text )

            if is_possible_space and character in whitespace_character:
                continue

            else:
                is_possible_space = False

            # Skip the next characters as we already know they are a list. This is only called when
            # the `comma_list_size` is lower than the `self._width`, otherwise the line will
            # be immediately flushed
            if comma_list_size > 0:
                comma_list_size     -= 1
                last_comma_list_size = comma_list_size + 1

                log( 4, "is_flushing, index %d accumulated_line_length %d comma_list_size %d comma_list_end_point %d character %s", index, accumulated_line_length, comma_list_size, comma_list_end_point, character )
                if not is_flushing_accumalated_line:

                    if not disable_line_wrapping_by_maximum_width \
                            and accumulated_line_length + next_word_length + indent_length > self._width:

                        force_flush_accumulated_line()

                    else:
                        accumulated_line      += character
                        is_flushing_comma_list = True
                        continue

            else:

                if last_comma_list_size == 1:

                    # It is not a comma separated list `if comma_separated_list_items_count < self.maximum_items_in_comma_separated_list`
                    # therefore we do not push a new line when flushing the processed contents by `is_comma_separated_list()`
                    if is_comma_separated_list:
                        force_flush_accumulated_line()

                    last_comma_list_size = 0

                is_flushing_comma_list  = False
                is_comma_separated_list = False

            log( 4, "%d %s ", index, character )
            if not disable_line_wrapping_by_maximum_width \
                    and not is_flushing_accumalated_line \
                    and accumulated_line_length + next_word_length + indent_length > self._width:

                force_flush_accumulated_line()

            if accumulated_line_length > minimum_line_size:
                is_allowed_to_wrap = True

            if self.is_word_separator_alpha(index, text, word_separator_characters) and is_allowed_to_wrap \
                    or is_flushing_accumalated_line:

                if index + 2 < text_length:
                    is_followed_by_space = text[index+1] in whitespace_character

                    if is_followed_by_space:

                        if not is_flushing_comma_list:

                            if self.is_word_separator_alpha(index, text, list_separator_characters):
                                is_comma_separated_list, comma_list_end_point, comma_separated_list_items_count = \
                                        self.is_comma_separated_list( text, index )

                                comma_list_size = comma_list_end_point - ( index + 1 )

                                if comma_separated_list_items_count < self.maximum_items_in_comma_separated_list:
                                    is_comma_separated_list = False

                            elif self.is_word_separator_alpha(index, text, word_separator_characters):
                                comma_list_size = -1
                                is_comma_separated_list = False

                        log( 4, "index %3d comma_list_size %d maximum_size %d",
                                index, comma_list_size, self.maximum_items_in_comma_separated_list,
                                'is_comma', is_comma_separated_list,
                                'is_flushing', is_flushing_accumalated_line )
                        if ( is_comma_separated_list \
                                and comma_list_size > -1 ) \
                                and not is_flushing_comma_list \
                                or ( not is_comma_separated_list and \
                                     comma_list_size < 0 ) \
                                or is_flushing_accumalated_line:

                            # It is not the first line anymore, now we need to use the `subsequent_indent_length`
                            indent_length = subsequent_indent_length

                            if character in whitespace_character:
                                character = ""

                            accumulated_line = "".join( [accumulated_line, character, "\n",
                                    ( "" if balance_characters_between_line_wraps else subsequent_indent ) ] )

                            log( 4, "accumulated_line flush %r", accumulated_line )
                            new_text.append( accumulated_line )

                            accumulated_line = ""
                            line_start_index = index + 1

                            is_possible_space            = True
                            is_allowed_to_wrap           = False
                            is_flushing_accumalated_line = False

                        else:
                            accumulated_line += character

                    else:
                        accumulated_line += character

                else:
                    accumulated_line += character

            else:
                accumulated_line += character

        # Flush out any remaining text
        if len( accumulated_line ):
            new_text.append(accumulated_line)

        log( 4, "new_text %s", new_text )
        return new_text

    def peek_next_word_length(self, index, text):
        match = next_word_pattern.match( text, index )

        if match:
            next_word = match.group(0)

            log( 4, "%r %s", next_word, len( next_word ) )
            return len( next_word )

        return 0

    def is_word_separator_alpha(self, index, text, checklisk):
        character = text[index]
        is_word_backboundary = False

        for separator in alpha_separator_characters:

            if character == separator[-1]:
                separator_length = len(separator)

                if index > separator_length:
                    for separator_index, separator_character in enumerate( reversed(separator) ):

                        if separator_character != text[index - separator_index]:
                            break

                    # This else is only run when the break statement is not raised or the list is empty!
                    else:
                        backboundary = (
                                text[index-separator_length] in whitespace_character
                                and text[index-separator_length-1] not in word_separator_characters
                                and index + 1 < len( text ) and not not spaces_pattern.match( text[index+1] )
                            )
                        is_word_backboundary = backboundary and character.isalpha() or not backboundary and not character.isalpha()
                        log( 4, separator, is_word_backboundary, backboundary )
                        break

        return is_word_backboundary or character in checklisk

    def is_comma_separated_list(self, text, index):
        """
            return if the next characters form a command separated list
            return 0 if False, otherwise the `text` index where the command separated list ended
        """
        log( 4, "index %d", index )
        comma_list_end_point = -1

        # A word list has at least 2 items. For example: start 1, 2, 3 words
        comma_separated_list_items_count = 2

        text_length   = len( text ) - 1
        words_counter = 0

        while index < text_length:
            index     = index + 1
            character = text[index]

            is_character_whitespace = character in whitespace_character

            if index < text_length:
                is_word_separator_character = self.is_word_separator_alpha(index, text, list_separator_characters)

                next_character = text[index+1]
                is_next_character_whitepace = next_character in whitespace_character

            else:
                next_character = '$'
                is_word_separator_character = character not in phrase_separator_characters
                is_next_character_whitepace = True

            # We count a word before it begins and set `comma_list_end_point` when we find a space after a comma
            if is_character_whitespace and not is_next_character_whitepace:
                words_counter += 1

            log( 4, "%d char %s next %s words %d", index, character, next_character, words_counter )
            if is_word_separator_character and is_next_character_whitepace:

                if 0 < words_counter < self.maximum_words_in_comma_separated_list:
                    comma_list_end_point = index

                    # When the next character is '$', we cannot count it as a item as it is already
                    # set by the `comma_separated_list_items_count` default value `2`
                    if next_character != '$':
                        comma_separated_list_items_count += 1

                else:
                    break

                words_counter = 0

        if comma_list_end_point > -1:
            log( 4, "True, end_point %d items_count %d", comma_list_end_point, comma_separated_list_items_count )
            return True, comma_list_end_point, comma_separated_list_items_count

        log( 4, "False, end_point %d items_count %d", 0, 0 )
        return False, 0, 0

    def classic_wrap_text(self, wrapper, paragraph_lines, initial_indent, subsequent_indent):
        orig_initial_indent = initial_indent
        orig_subsequent_indent = subsequent_indent

        if orig_initial_indent or orig_subsequent_indent:
            # Textwrap is somewhat limited.  It doesn't recognize tabs
            # in prefixes.  Unfortunately, this means we can't easily
            # differentiate between the initial and subsequent.  This
            # is a workaround.
            initial_indent = orig_initial_indent.expandtabs(self._tab_width)
            subsequent_indent = orig_subsequent_indent.expandtabs(self._tab_width)
            wrapper.initial_indent = initial_indent
            wrapper.subsequent_indent = subsequent_indent

        text = '\n'.join(paragraph_lines)
        text = text.expandtabs(self._tab_width)
        text = wrapper.fill(text)

        # Put the tabs back to the prefixes.
        if orig_initial_indent or orig_subsequent_indent:

            if (initial_indent != orig_subsequent_indent
                or subsequent_indent != orig_subsequent_indent
                ):
                lines = text.splitlines()

                if initial_indent != orig_initial_indent:
                    log( 2, 'fix tabs %r', lines[0])
                    lines[0] = orig_initial_indent + lines[0][len(initial_indent):]
                    log( 2, 'new line is %r', lines[0])

                if subsequent_indent != orig_subsequent_indent:

                    for index, line in enumerate(lines[1:]):
                        remaining = lines[index + 1][len(subsequent_indent):]
                        lines[index + 1] = orig_subsequent_indent + remaining

                text = '\n'.join(lines)

        return text


last_used_width = 80

class WrapLinesEnhancementAskCommand(sublime_plugin.TextCommand):

    def run(self, edit, line_wrap_type=None):
        self.line_wrap_type = line_wrap_type

        view = sublime.active_window().show_input_panel(
            'Provide wrapping width', str( last_used_width ),
            self.input_package, None, None
        )
        view.run_command("select_all")

    def input_package(self, width):
        global last_used_width

        last_used_width = width
        self.view.run_command( 'wrap_lines_plus', { 'width': int( width ), "line_wrap_type": self.line_wrap_type } )

