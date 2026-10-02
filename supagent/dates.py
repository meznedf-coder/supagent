"""Month names as dates are written in questions and answers, English and French, whole or abbreviated
(September, Sept., sep, septembre, févr., août): a word that only starts like one (markets, decisions, junior,
octets, novices, maintenance) is not a month, so "the top 5 markets" names no day."""

MONTH_WORDS = (r"january|february|march|april|may|june|july|august|september|october|november|december|"
               r"janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre|"
               r"janv|f[ée]vr|f[ée]v|avr|juil|d[ée]c|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec")
MONTH_END = r"\.?(?![a-zà-ÿ])"                     # "Sept." or "sep" then no other letter
MONTH = rf"(?:{MONTH_WORDS}){MONTH_END}"           # no group: for patterns that only find dates
