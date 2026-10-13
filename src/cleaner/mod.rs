//! Text sanitization for track metadata.
//!
//! Strips common "noise" found in YouTube/Spotify titles such as
//! `[Official Video]`, `(Remastered 2024)`, `(Audio)`, `feat. Someone`,
//! and collapses/truncates the result to a configurable maximum length.

use once_cell::sync::Lazy;
use regex::{Regex, RegexBuilder};

/// Built-in patterns removed from every title, in order.
/// Kept as (description, pattern) pairs mainly for documentation/tests.
static BUILTIN_PATTERNS: Lazy<Vec<Regex>> = Lazy::new(|| {
    let raw_patterns: &[&str] = &[
        // Bracketed/parenthesized tags: [Official Video], (Official Music Video),
        // [Lyrics], (HD), [4K], (Visualizer), etc.
        r"(?i)[\[\(]\s*official\s*(music\s*)?(video|audio|lyric[s]?)?\s*[\]\)]",
        r"(?i)[\[\(]\s*(lyric[s]?( video)?|visualizer|hd|4k|hq)\s*[\]\)]",
        // (Remastered), (Remastered 2024), (2024 Remaster), (Remaster)
        r"(?i)[\[\(]\s*\d{0,4}\s*re-?master(ed)?\s*\d{0,4}\s*[\]\)]",
        // Bare "- Remastered 2015" / "- Remaster" without brackets (common on YouTube)
        r"(?i)\s*[-–—]\s*\d{0,4}\s*re-?master(ed)?\s*\d{0,4}\b",
        // (Audio), [Audio Only]
        r"(?i)[\[\(]\s*audio(\s*only)?\s*[\]\)]",
        // Live performance / quality-upgrade tags: [Live], [4K Upgrade], [HD Upgrade], [Upgrade]
        r"(?i)[\[\(]\s*live\s*[\]\)]",
        r"(?i)[\[\(]\s*(4k|8k|hd|uhd|fhd)?\s*upgrade\s*[\]\)]",
        // (Explicit), (Clean), (Radio Edit), (Extended Mix), (Album Version)
        r"(?i)[\[\(]\s*(explicit|clean|radio edit|extended( mix)?|album version|single version|mono|stereo)\s*[\]\)]",
        // feat./ft./featuring ... up to the next bracket, dash, or end of string
        r"(?i)\s*[\(\[]?\s*(feat\.?|ft\.?|featuring)\s+[^()\[\]\-]+[\)\]]?",
        // Leftover empty brackets after previous substitutions, e.g. "()" "[]"
        r"[\(\[]\s*[\)\]]",
    ];
    raw_patterns
        .iter()
        .map(|p| RegexBuilder::new(p).build().expect("invalid built-in regex"))
        .collect()
});

/// Collapses repeated whitespace into a single space.
static WHITESPACE: Lazy<Regex> = Lazy::new(|| Regex::new(r"\s+").unwrap());

/// Trailing separators left dangling after a removal, e.g. "Song -  " or "Song ,".
static TRAILING_SEP: Lazy<Regex> = Lazy::new(|| Regex::new(r"[\s\-,–—]+$").unwrap());
static LEADING_SEP: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[\s\-,–—]+").unwrap());

/// Sanitizes a single metadata field (title, artist, album, ...).
///
/// Applies the built-in noise patterns, then any `extra_patterns` supplied by
/// the user's config (each treated as a case-sensitive regex; matches are
/// removed), collapses whitespace, and trims dangling separators.
pub fn clean_field(input: &str, extra_patterns: &[String]) -> String {
    let mut text = input.to_string();

    for re in BUILTIN_PATTERNS.iter() {
        text = re.replace_all(&text, "").to_string();
    }

    for pattern in extra_patterns {
        if let Ok(re) = Regex::new(pattern) {
            text = re.replace_all(&text, "").to_string();
        }
    }

    text = WHITESPACE.replace_all(&text, " ").trim().to_string();
    text = TRAILING_SEP.replace_all(&text, "").to_string();
    text = LEADING_SEP.replace_all(&text, "").to_string();
    text.trim().to_string()
}

/// Removes the artist name from the start and/or end of a title when it's
/// redundant, e.g. turns "Linkin Park - Numb / Encore - Linkin Park / JAY-Z"
/// (artist = "Linkin Park") into "Numb / Encore".
///
/// Handles:
/// - Leading "Artist - " / "Artist: " prefixes.
/// - Trailing "- Artist" suffixes, optionally followed by a "/"- or
///   ","-separated list of additional (featured) artists, since YouTube
///   often appends the full credited artist list at the end of the title.
///
/// Matching is case-insensitive and only removes *exact* occurrences of the
/// given artist name (not arbitrary substrings), so it won't accidentally
/// eat unrelated text.
pub fn strip_redundant_artist(title: &str, artist: &str) -> String {
    let artist = artist.trim();
    if artist.is_empty() {
        return title.to_string();
    }

    let escaped = regex::escape(artist);
    let mut result = title.to_string();

    // "Artist - Title" / "Artist: Title" at the very start.
    if let Ok(leading) = Regex::new(&format!(r"(?i)^\s*{escaped}\s*[-–—:]\s*")) {
        result = leading.replace(&result, "").to_string();
    }

    // "Title - Artist", optionally with extra "/ Other Artist" credits tacked
    // on at the very end.
    if let Ok(trailing) = Regex::new(&format!(
        r"(?i)\s*[-–—]\s*{escaped}(\s*[/,&]\s*[^/,&]+)*\s*$"
    )) {
        result = trailing.replace(&result, "").to_string();
    }

    WHITESPACE.replace_all(result.trim(), " ").to_string()
}

/// Full cleaning pipeline for a track title: strips noise tags, then removes
/// a redundant leading/trailing artist credit, then trims dangling separators.
pub fn clean_title(title: &str, artist: &str, extra_patterns: &[String]) -> String {
    let cleaned = clean_field(title, extra_patterns);
    let deduped = strip_redundant_artist(&cleaned, artist);
    let trimmed = TRAILING_SEP.replace_all(&deduped, "").to_string();
    LEADING_SEP.replace_all(&trimmed, "").trim().to_string()
}


/// UTF-8 boundaries, and appends an ellipsis ("…") when truncation occurs.
/// If `max_len` is 0, returns an empty string.
pub fn truncate(input: &str, max_len: usize) -> String {
    if max_len == 0 {
        return String::new();
    }
    let char_count = input.chars().count();
    if char_count <= max_len {
        return input.to_string();
    }
    if max_len == 1 {
        return "…".to_string();
    }
    let truncated: String = input.chars().take(max_len - 1).collect();
    format!("{}…", truncated.trim_end())
}

/// Renders the final chatbox string from a template and cleaned fields, then
/// truncates it to `max_len`.
pub fn render_template(
    template: &str,
    artist: &str,
    title: &str,
    album: &str,
    status: &str,
    volume: &str,
    max_len: usize,
) -> String {
    let rendered = template
        .replace("{artist}", artist)
        .replace("{title}", title)
        .replace("{album}", album)
        .replace("{status}", status)
        .replace("{volume}", volume);
    let rendered = WHITESPACE.replace_all(rendered.trim(), " ").to_string();
    truncate(&rendered, max_len)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn removes_official_video_tag() {
        assert_eq!(
            clean_field("Never Gonna Give You Up [Official Video]", &[]),
            "Never Gonna Give You Up"
        );
        assert_eq!(
            clean_field("Never Gonna Give You Up (Official Music Video)", &[]),
            "Never Gonna Give You Up"
        );
    }

    #[test]
    fn removes_remastered_tag() {
        assert_eq!(
            clean_field("Come Together (Remastered 2024)", &[]),
            "Come Together"
        );
        assert_eq!(
            clean_field("Come Together (2024 Remaster)", &[]),
            "Come Together"
        );
        assert_eq!(clean_field("Come Together - Remaster", &[]), "Come Together");
    }

    #[test]
    fn removes_audio_tag() {
        assert_eq!(clean_field("Some Song (Audio)", &[]), "Some Song");
        assert_eq!(clean_field("Some Song [Audio Only]", &[]), "Some Song");
    }

    #[test]
    fn removes_featuring_credit() {
        assert_eq!(
            clean_field("Blinding Lights feat. Someone", &[]),
            "Blinding Lights"
        );
        assert_eq!(
            clean_field("Blinding Lights (ft. Someone Else)", &[]),
            "Blinding Lights"
        );
        assert_eq!(
            clean_field("Blinding Lights featuring The Weeknd", &[]),
            "Blinding Lights"
        );
    }

    #[test]
    fn removes_multiple_tags_at_once() {
        assert_eq!(
            clean_field(
                "Song Title (feat. Guest) [Official Video] (Remastered 2024)",
                &[]
            ),
            "Song Title"
        );
    }

    #[test]
    fn leaves_clean_titles_untouched() {
        assert_eq!(clean_field("Bohemian Rhapsody", &[]), "Bohemian Rhapsody");
    }

    #[test]
    fn applies_extra_user_patterns() {
        let extra = vec![r"(?i)HD Rip".to_string()];
        assert_eq!(clean_field("Movie Song HD Rip", &extra), "Movie Song");
    }

    #[test]
    fn ignores_invalid_extra_pattern() {
        let extra = vec!["(unclosed".to_string()];
        // Should not panic, invalid regex is simply skipped.
        assert_eq!(clean_field("Some Song", &extra), "Some Song");
    }

    #[test]
    fn removes_live_and_upgrade_tags() {
        assert_eq!(clean_field("Numb [Live]", &[]), "Numb");
        assert_eq!(clean_field("Numb [4K Upgrade]", &[]), "Numb");
        assert_eq!(clean_field("Numb [HD Upgrade]", &[]), "Numb");
        assert_eq!(clean_field("Numb [Upgrade]", &[]), "Numb");
    }

    #[test]
    fn strips_leading_artist_credit() {
        assert_eq!(
            strip_redundant_artist("Linkin Park - Numb", "Linkin Park"),
            "Numb"
        );
    }

    #[test]
    fn strips_trailing_artist_credit_with_extra_names() {
        assert_eq!(
            strip_redundant_artist("Numb / Encore - Linkin Park / JAY-Z", "Linkin Park"),
            "Numb / Encore"
        );
    }

    #[test]
    fn leaves_title_untouched_when_artist_not_present() {
        assert_eq!(
            strip_redundant_artist("Bohemian Rhapsody", "Queen"),
            "Bohemian Rhapsody"
        );
    }

    #[test]
    fn clean_title_full_pipeline_matches_reported_bug() {
        // Real-world YouTube title reported by the user.
        let raw = "Linkin Park - Numb / Encore [Live] [4K Upgrade] - Linkin Park / JAY-Z";
        let cleaned = clean_title(raw, "Linkin Park", &[]);
        assert_eq!(cleaned, "Numb / Encore");
    }

    #[test]
    fn clean_title_handles_no_artist_metadata_gracefully() {
        let raw = "Some Song [Live] [4K Upgrade]";
        let cleaned = clean_title(raw, "", &[]);
        assert_eq!(cleaned, "Some Song");
    }

    #[test]
    fn truncate_respects_char_boundaries_and_unicode() {
        let s = "🎵".repeat(10);
        let result = truncate(&s, 5);
        assert_eq!(result.chars().count(), 5);
        assert!(result.ends_with('…'));
    }

    #[test]
    fn truncate_noop_when_short_enough() {
        assert_eq!(truncate("hello", 140), "hello");
    }

    #[test]
    fn truncate_zero_length() {
        assert_eq!(truncate("hello", 0), "");
    }

    #[test]
    fn render_template_basic() {
        let out = render_template(
            "🎵 {artist} - {title}",
            "Queen",
            "Bohemian Rhapsody",
            "A Night at the Opera",
            "Playing",
            "🔊 50%",
            140,
        );
        assert_eq!(out, "🎵 Queen - Bohemian Rhapsody");
    }

    #[test]
    fn render_template_with_volume() {
        let out = render_template(
            "🎵 {artist} - {title} [{volume}]",
            "Queen",
            "Bohemian Rhapsody",
            "A Night at the Opera",
            "Playing",
            "🔊 50%",
            140,
        );
        assert_eq!(out, "🎵 Queen - Bohemian Rhapsody [🔊 50%]");
    }

    #[test]
    fn render_template_truncates_long_output() {
        let long_title = "A".repeat(200);
        let out = render_template("{title}", "", &long_title, "", "", "", 140);
        assert_eq!(out.chars().count(), 140);
        assert!(out.ends_with('…'));
    }
}
