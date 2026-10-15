from kurdiptv.normalize import (
    display_name,
    fix_mojibake,
    fold_ascii,
    is_token_subset,
    latin_name_in_mixed_title,
    name_key,
    shorten_seo_title,
    similarity,
)


def test_mojibake_is_repaired():
    assert fix_mojibake("TRT Ã‡ocuk") == "TRT Çocuk"
    assert fix_mojibake("Ã‡ira TV HD") == "Çira TV HD"
    assert fix_mojibake("TÃ¼rkmeneli TV") == "Türkmeneli TV"
    assert fix_mojibake("Plain ASCII") == "Plain ASCII"
    assert fix_mojibake("Çira TV") == "Çira TV"  # genuine text is left alone


def test_case_punctuation_and_whitespace_do_not_matter():
    assert name_key("RUDAW  tv") == name_key("rudaw-TV!") == "rudaw"


def test_quality_and_live_suffixes_are_ignored():
    for variant in ["Rudaw TV HD", "Rudaw TV FHD", "Rudaw Live", "Rudaw TV ZINDÎ", "Rudaw TV 1080p", "Rudaw 4K", "RUDAW TV Live Stream"]:
        assert name_key(variant) == "rudaw", variant


def test_backup_markers_are_ignored():
    assert name_key("Kurdsat Tv (Alt 2)") == name_key("Kurdsat TV") == "kurdsat"
    assert name_key("Sterk TV [Backup]") == "sterk"
    assert name_key("Sterk TV (2)") == "sterk"


def test_kurdish_and_turkish_letters_fold_to_ascii():
    assert name_key("Çira TV") == name_key("Cira TV") == "cira"
    assert name_key("Ronahî TV") == name_key("Ronahi TV") == "ronahi"
    assert name_key("TRT Kurdî") == name_key("TRT Kurdi") == "trt kurdi"
    assert fold_ascii("Şêrko Ûmît ıİ") == "serko umit ii"


def test_arabic_script_variants_fold_together():
    # Sorani vs Arabic spellings of the same word
    assert name_key("ڕووداو") == name_key("رووداو")
    assert name_key("كوردستان") == name_key("کوردستان")
    assert name_key("کوردستان ٢٤") == name_key("کوردستان 24")  # Arabic-Indic digits


def test_digits_are_kept_and_split_from_letters():
    assert name_key("Kurdistan24") == name_key("Kurdistan 24") == "kurdistan 24"
    assert name_key("NRT2 HD") == name_key("NRT 2") == "nrt 2"
    assert name_key("Kurdistan TV") != name_key("Kurdistan 24")
    assert name_key("TV 10") == "10" or name_key("TV 10") == "tv 10"  # a bare number keeps its meaning


def test_distinguishing_qualifiers_are_kept():
    assert name_key("Kurdmax Sorani") != name_key("Kurdmax Kurmanci")
    assert name_key("Kurdmax Show") != name_key("Kurdmax Music")
    assert name_key("Zarok TV Kurmanci") != name_key("Zarok TV Sorani")


def test_noise_only_name_still_yields_something():
    assert name_key("TV") == "tv" or name_key("TV") == ""


def test_seo_titles_are_shortened():
    assert shorten_seo_title("ARYEN TV Kurdish Unveiling Kurdish Culture and Media Tenacity") == "ARYEN TV"
    assert shorten_seo_title("Duhok TV Sports is a Kurdish sports channel") == "Duhok TV"
    assert shorten_seo_title("Jin TV | Mafên Jinên Kurd Di Navbera Çanda û Civaka Kurdî de") == "Jin TV"
    assert shorten_seo_title("4 Kurd Live Online – Kurdish TV Channels") == "4 Kurd Live Online"
    assert shorten_seo_title("Kurdistan 24 TV Live 4K") == "Kurdistan 24 TV Live 4K"  # short titles are untouched
    assert name_key("Speda TV The Voice of Kurdish Society and Culture") == "speda"


def test_latin_channel_name_is_pulled_from_mixed_script_titles():
    assert latin_name_in_mixed_title("عالم الموسيقى الكردية مع قناة Med Muzik TV") == "Med Muzik TV"
    assert latin_name_in_mixed_title("قەڵەم – پەخشی ڕاستەوخۆ لە Qallam Ainy TV Live") == "Qallam Ainy TV Live"
    assert latin_name_in_mixed_title("Rudaw TV") is None
    assert latin_name_in_mixed_title("البث المباشر قناة دجلة") is None


def test_display_name_is_tidy():
    assert display_name("Befrin hd") == "Befrin"
    assert display_name("Rudaw TV ZINDÎ") == "Rudaw TV"
    assert display_name("Sterk TV (Alt 2)") == "Sterk TV"
    assert display_name("TRT Ã‡ocuk") == "TRT Çocuk"


def test_similarity_helpers():
    assert similarity("rudaw", "rudaw") == 1.0
    assert similarity("rudaw", "kurdsat") < 0.6
    assert is_token_subset("zarok", "zarok kurmanci")
    assert not is_token_subset("zarok", "zarok")
    assert not is_token_subset("kurdistan", "kurdsat")
