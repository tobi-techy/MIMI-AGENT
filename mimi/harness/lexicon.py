"""The router's fast path: multilingual phrase rules, language and injection checks.

Plain code, no model, microseconds per message. It covers the messages shops
send most (balances, "I don pay", taps, YES, orders, greetings) in English,
Nigerian Pidgin, Yoruba, Hausa and Igbo, with common typos. When the phrases
do not settle a message clearly (nothing matched, or two skills close), the
router asks the model; the phrases never guess.

Each rule is ``(skill key, weight, pattern)``. A message's score for a skill
is the sum of the weights of its matching rules; the router trusts the top
skill only when it clears ``STRONG`` and leads the next one by ``MARGIN``.
Weights: 3 a phrase that settles the skill on its own, 2 strong evidence,
1 a hint.

The Yoruba, Hausa and Igbo phrases were written for the synthetic eval and
need a native-speaker review before the pilot (the same open item as the
receipt and voice templates).
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass

STRONG = 3.0
MARGIN = 2.0

LANGS = ("en", "pcm", "yo", "ha", "ig")


def fold(text: str) -> str:
    """Lowercase, strip diacritics (Yoruba/Igbo tone marks), tidy quotes and spaces."""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    t = t.lower()
    t = re.sub(r"[\u200b-\u200f]", "", t)
    return re.sub(r"\s+", " ", t).strip()


# Common SMS spellings, mapped before the phrase rules run (not before the
# language check). Kept short and generic; the model handles the rest.
_SMS = {
    "hw": "how", "mch": "much", "wen": "when", "whn": "when", "d": "the", "u": "you",
    "ur": "your", "pls": "please", "plz": "please", "wat": "what", "incrase": "increase",
    "limt": "limit", "ammount": "amount",
}
_SMS_RX = re.compile(r"\b(" + "|".join(map(re.escape, _SMS)) + r")\b")


def normalise(text: str) -> str:
    return _SMS_RX.sub(lambda m: _SMS[m.group(1)], fold(text))


# ------------------------------------------------------------------ rules

QTY = r"(?:\d+|one|two|three|four|five|ten|ikan|meji|meta|merin|marun|daya|biyu|uku|hudu|biyar|otu|abuo|ato|ano|ise)"
UNIT = r"(?:cartons?|ctns?|katon|kwali|paali|bags?|packs?|crates?|pieces?|pcs|tins?|sachets?|rolls?|dozens?|bales?)"
PAY_V = r"(?:pay|paid|payed|send|sent|transfer(?:red)?|trf|tf)"

_R: list[tuple[str, float, str]] = [
    # ---------------- collector/collections (balance and amount disputes)
    ("collector/collections", 3, r"\bhow much (?:do |did )?(?:i|we) (?:still )?(?:owe|ow|0we)\b"),
    ("collector/collections", 3, r"\bwhat (?:do |did )?(?:i|we) (?:still )?owe\b"),
    ("collector/collections", 3, r"\b(?:my|our) (?:current |total )?(?:balance|outstanding|debt)\b"),
    ("collector/collections", 3, r"\bwetin (?:i|we) (?:still )?(?:owe|dey owe|go pay)\b"),
    ("collector/collections", 3, r"\bhow much (?:i|we) (?:still )?(?:owe|dey owe|ow)\b"),
    ("collector/collections", 3, r"\bhow much (?:(?:e|i|we) )?(?:still )?remain(?:s|ing)?\b(?! (?:for|on) (?:the |my )?shelf)"),
    ("collector/collections", 2, r"\b(?:remaining|outstanding) (?:balance|amount)\b"),
    ("collector/collections", 3, r"\b(?:send|give|drop|wetin be|what is|which) (?:me )?(?:una |your |the )?(?:account|acct|acc) ?(?:number|no\.?|details?)?\b"),
    ("collector/collections", 3, r"\bwhere (?:do |should |can )?(?:i|we) (?:go )?pay (?:to|into)?\b"),
    ("collector/collections", 3, r"\b(?:the |that |this )?amount (?:is |no )?(?:wrong|not correct|incorrect|too much|no correct)\b"),
    ("collector/collections", 3, r"\bi (?:no|don't|do not|dont) owe\b"),
    ("collector/collections", 2, r"\b(?:statement|balance)\b"),
    ("collector/collections", 3, r"\b(?:elo|melo) (?:ni|lo) (?:mo|a) (?:je|jẹ|ku)\b|\bowo (?:ti )?mo je\b|\bgbese mi\b|\belo lo ku\b"),
    ("collector/collections", 4, r"\b(?:owo )?(?:elo|melo) lo ku\b"),
    ("collector/collections", 3, r"\bnawa (?:nake|ake|muke) (?:bi|ci)\b|\bbashi na nawa\b|\bnawa ya rage\b|\blambar (?:asusu|account)\b"),
    ("collector/collections", 3, r"\bego ole ka (?:m|anyi) (?:ji|ruru)\b|\bole ka m ji\b|\bugwo m\b"),
    ("collector/collections", 3, r"\bhow much (?:does|do) .{2,40} owe\b|\bwhat does .{2,40} owe\b|\bhow much is .{2,40} owing\b"),
    ("collector/collections", 3, r"\b(?:balance|outstanding) (?:of|for) \w"),
    # ---------------- collector/payment-check
    ("collector/payment-check", 3, rf"\bi(?:'ve| have| has)? (?:already |just )?{PAY_V}\b(?! (?:you|una) (?:next|tomorrow|on|by|later))"),
    ("collector/payment-check", 3, r"\bi (?:don|done|dun) (?:pay|paid|send|transfer|sent)\b"),
    ("collector/payment-check", 3, r"\b(?:payment|transfer|money) (?:is |has been |don )?(?:done|made|sent|gone|go|enter)\b"),
    ("collector/payment-check", 3, r"\b(?:gave|give|handed|don give) (?:the |your |una )?(?:rep|driver|boy|sales ?man|marketer) (?:the )?(?:cash|money|\d)"),
    ("collector/payment-check", 3, r"\b(?:confirm|check) (?:my |the |am |if (?:my )?)?(?:payment|transfer|money)\b|\babeg confirm\b"),
    ("collector/payment-check", 3, r"\btransfer (?:don |has |have )?(?:land(?:ed)?|enter|reach)\b"),
    ("collector/payment-check", 3, r"\b(?:did|has) .{2,40} (?:pay|paid)\b|\b(?:did|has) .{2,40} (?:payment|transfer|money) (?:land|come|enter|arrive|clear)\b"),
    ("collector/payment-check", 2, r"\bpaid (?:in )?(?:full|part|half|\d)"),
    ("collector/payment-check", 3, r"\b(?:pay|paid|don pay|give) (?:me|us|the rep) .{0,20}(?:cash|\d+k?\b)"),
    ("collector/payment-check", 3, r"\bmo ti (?:san|sanwo|fi owo ranse|transfer)\b|\bmo san owo\b|\bmo ti fun (?:rep|driver)\b"),
    ("collector/payment-check", 3, r"\bna (?:biya|tura (?:kudi|kudin)|aika (?:kudi|kudin))\b|\bna ba (?:rep|yaron ku|direba) kudi\b"),
    ("collector/payment-check", 3, r"\bakwu(?:o)?la m\b|\beziteela m ego\b|\bezitela m ego\b|\bezigara m ego\b|\benyere m (?:rep|onye) ego\b"),
    # ---------------- collector/promise-to-pay
    ("collector/promise-to-pay", 3, r"\bi(?:'ll| will| go| shall) (?:pay|send|transfer|clear|balance)\b"),
    ("collector/promise-to-pay", 3, r"\bwill (?:pay|send|clear) (?:it |am |the (?:money|balance) )?(?:on|by|next|tomorrow|today|this|later|after|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"),
    ("collector/promise-to-pay", 3, r"\bgive me (?:small |some |more )?(?:time|days|till|until|chance)\b"),
    ("collector/promise-to-pay", 3, r"\bmoney (?:will|go) (?:come|enter|land)\b|\bwhen money (?:enter|come)\b"),
    ("collector/promise-to-pay", 2, r"\b(?:end of (?:the )?month|month end|after market day|next week|by friday)\b"),
    ("collector/promise-to-pay", 2, r"\bmake i (?:pay|send)\b|\bwait (?:small|till|until)\b"),
    ("collector/promise-to-pay", 3, r"\b(?:ma|maa|mo ma|mo maa|emi yoo) (?:san|sanwo|fi (?:owo )?ranse)\b|\be fun mi (?:ni )?(?:akoko|ojo)\b|\blola ni ma san\b"),
    ("collector/promise-to-pay", 3, r"\bzan (?:biya|tura|aika)\b|\bku ba(?:ni|ni ni) lokaci\b"),
    ("collector/promise-to-pay", 3, r"\baga m (?:akwu|ezi|kwuo)\b|\bnye m oge\b|\bechi ka m ga\b"),
    # ---------------- collector/chase (owner)
    ("collector/chase", 3, r"\bchase\b|\bremind (?:everyone|everybody|all|them|the shops|debtors)\b"),
    ("collector/chase", 3, r"\bwho (?:still )?(?:owes|dey owe|is owing)\b|\bdebtors?\b|\boverdue (?:list|shops|accounts)\b"),
    ("collector/chase", 3, r"\bsend (?:out )?reminders\b|\bcollect (?:my |our )?money from\b"),
    ("collector/chase", 3, r"\b(?:gentle|polite|soft|friendly) reminders?\b|\bremind (?:the )?(?:\w+ )?shops\b"),
    ("collector/chase", 3, r"\bfollow up (?:the ones|those|everybody|everyone|all)\b"),
    ("collector/chase", 2, r"\bremind (?:the )?(?:\w+ )?shops\b.{0,30}\b(?:balance|owe|debt|money)\b"),
    # ---------------- proof/fake-alert-check (text alerts, screenshots in words)
    ("proof/fake-alert-check", 3, r"\b(?:acct|a/c|acc)\s*[:#]?\s*[\*x]{2,}\d{2,}"),
    ("proof/fake-alert-check", 3, r"\b(?:credit|cr) alert\b|\balert (?:don|has|have) (?:come|enter|drop)\b|\bdebit alert\b"),
    ("proof/fake-alert-check", 2, r"\b(?:amt|amount)\s*[:=]\s*(?:ngn|n|₦)?\s?[\d,]+"),
    ("proof/fake-alert-check", 3, r"\bscreen ?shot\b|\bproof of (?:payment|transfer)\b|\bsee (?:the |my )?(?:proof|receipt|evidence)\b"),
    ("proof/fake-alert-check", 2, r"\b(?:check|confirm) (?:this|the|my) (?:alert|receipt)\b|\breceipt (?:attached|dey)\b"),
    # ---------------- order/order-intake
    ("order/order-intake", 3, rf"\b(?:i (?:want|need|wan)|we (?:want|need)|send|bring|supply|deliver|carry|add) (?:me |us |am )?(?:\w+ ){{0,3}}{QTY} {UNIT}\b"),
    ("order/order-intake", 3, rf"\b{QTY} {UNIT} (?:of )?\w+.{{0,30}}\b(?:please|abeg|biko|pls|plz|don allah|for (?:my|next) (?:order|delivery))\b"),
    ("order/order-intake", 3, r"\b(?:i want to|i wan|we wan|i wan come) (?:order|buy|collect|restock)\b|\brestock (?:me|us|my)\b|\bplace (?:an )?order\b"),
    ("order/order-intake", 3, r"\b(?:add|put) .{2,40} (?:to|for|for my|for the|in) (?:my |the |next )?(?:order|delivery)\b"),
    ("order/order-intake", 2, r"\b(?:do you have|una get|e dey (?:available)?\?|is .{2,30} available|you get)\b"),
    ("order/order-intake", 3, rf"\b(?:price (?:of|for)|how much (?:is|be|for) (?:one |a |1 )?{UNIT})\b"),
    ("order/order-intake", 3, rf"\b(?:wants?|needs?) {QTY} {UNIT}\b"),
    ("order/order-intake", 3, r"\bmo fe (?:ra|order|gba)\b|\be (?:mu|gbe) .{2,30} wa\b|\bse e ni\b"),
    ("order/order-intake", 3, r"\bina so (?:in sayi|a kawo|ku kawo|kaya)\b|\ba kawo min\b|\bku kawo min\b"),
    # Repeat orders, pay-on-delivery orders, Yoruba "help me bring/buy" (RAI-302).
    ("order/order-intake", 3, r"\b(?:e |e jowo |jowo |ejo )?(?:ba mi|fun mi) (?:mu|gbe|ra|fi|ko)\b"),
    ("order/order-intake", 3, r"\brepeat (?:my |the |our )?(?:last |usual |previous )?order\b|\bsame (?:order|thing|things|goods|one)s? (?:as |like )?(?:last|before|the last|usual|previous)\b|\bsame as (?:last|before|the last|usual|previous)\b"),
    ("order/order-intake", 3, r"\b(?:pay|cash|transfer)\b.{0,25}\b(?:when|once|on|before)\b.{0,15}\b(?:deliver\w*|(?:the )?van|una (?:bring|deliver)|you (?:bring|deliver))\b"),
    ("order/order-intake", 3, r"\bachoro m (?:izu|ka unu bute)\b|\bbute (?:\w+ ){0,3}(?:carton|katon)\b|\bunu nwere\b"),
    # ---------------- order/drop-confirmation
    ("order/drop-confirmation", 3, r"^(?:yes|yes o|yes sir|yes ma|yeah|yep|received|confirmed|ok received|i confirm)[.! ]*$"),
    ("order/drop-confirmation", 3, r"\b(?:goods|items|load|delivery|order|everything) (?:don |has |have )?(?:been )?(?:received|complete|arrive[d]?|reach(?:ed)?|land(?:ed)?|come)\b"),
    ("order/drop-confirmation", 3, r"\b(?:got|received|receive|collect(?:ed)?) (?:the |my |all (?:the )?)?(?:goods|delivery|items|load|order|everything)\b"),
    ("order/drop-confirmation", 3, rf"\b{QTY} {UNIT} (?:short|missing|no reach|no complete|remain|broken|damaged|spoil(?:t|ed)?|expired)\b"),
    ("order/drop-confirmation", 3, r"\b(?:short(?:ed)? me|una short|not complete|no complete|e no complete|incomplete|damaged|broken|wrong (?:item|goods|product)|spoilt|leaking|expired goods)\b"),
    ("order/drop-confirmation", 3, r"\bi (?:don|done) (?:receive|collect|get) (?:am|the goods|the load|everything|am complete)\b|\be (?:don )?complete\b"),
    ("order/drop-confirmation", 3, r"\bmo ti gba (?:oja|eru|e|won)\b|\bo pe\b|\bko pe\b|\bo ti baje\b"),
    ("order/drop-confirmation", 3, r"\bna karba\b|\bkaya sun (?:iso|zo)\b|\bbai cika ba\b|\bya lalace\b|\bsun ragu\b"),
    ("order/drop-confirmation", 3, r"\banata(?:la)? m\b|\benwetala m\b|\bo zughi\b|\bemebiri\b"),
    # ---------------- order/delivery-status
    ("order/delivery-status", 3, r"\bwhen (?:is|will|go|una|una go|e go|the) ?(?:the )?(?:van|truck|motor|driver|delivery|goods|order|rep|bus)?\s?(?:go )?(?:come|coming|arrive|reach|land|deliver)\b"),
    ("order/delivery-status", 3, r"\bwhere (?:is|be|are) (?:my|the|our) (?:order|delivery|van|goods|motor|driver|load)\b|\bwhere my goods\b"),
    ("order/delivery-status", 3, r"\b(?:goods|order|delivery|load|van|motor) (?:has |have |is )?(?:never|not|no|still not|neva) (?:yet )?(?:arrive[d]?|deliver(?:ed)?|come|reach|land)\b"),
    ("order/delivery-status", 3, r"\bi (?:still )?(?:dey|am) (?:wait|waiting) (?:for|since)\b|\bstill waiting\b|\bhaven't (?:received|seen) (?:my|the|any)\b|\bnever bring\b"),
    ("order/delivery-status", 3, r"\bnigba wo ni (?:oja|moto|van|e)\b|\boja mi ko ti de\b"),
    ("order/delivery-status", 3, r"\bwhat time (?:una|you|the van|the driver|una van) (?:dey |go |will )?(?:come|reach|arrive)\b|\bbring my goods\b"),
    ("order/delivery-status", 3, r"\byaushe (?:motar|kaya|za ku)\b|\bkaya ba su iso ba\b|\bina kayana\b"),
    ("order/delivery-status", 3, r"\bolee mgbe\b|\bngwa m abia(?:beghi)?\b"),
    # ---------------- credit/credit-request
    ("credit/credit-request", 3, r"\b(?:increase|raise|extend|bump|add|up|expand) (?:my |our |the )?(?:credit|limit|credit limit)\b"),
    ("credit/credit-request", 3, r"\bmore credit\b|\bon credit\b|\bcredit me\b|\bpay later\b|\bi go pay (?:you )?later\b"),
    ("credit/credit-request", 3, r"\b(?:give|supply|bring) (?:me |us )?(?:the )?goods (?:first|now|today).{0,30}(?:pay|balance) (?:later|after|when)\b"),
    ("credit/credit-request", 2, r"\bcredit limit\b|\bmy limit\b"),
    ("credit/credit-request", 3, r"\be fi kun (?:credit|limit)\b|\blori gbese\b"),
    ("credit/credit-request", 3, r"\bku kara min (?:bashi|credit|limit)\b|\bku bani bashi\b|\bkaya bashi\b"),
    ("credit/credit-request", 3, r"\bbulite (?:credit|limit)\b|\bna mgbaze\b"),
    # ---------------- credit/credit-review (owner)
    ("credit/credit-review", 3, r"\b(?:should|can|shall) (?:i|we) (?:give|extend|increase|raise|allow) .{0,40}(?:credit|limit)\b"),
    ("credit/credit-review", 3, r"\bis .{2,40} (?:safe|risky|trustworthy|reliable|a risk)\b|\brisk (?:on|for|of) \w"),
    ("credit/credit-review", 3, r"\breview (?:the )?(?:credit|limits?)\b|\bcredit (?:check|review|limit for)\b"),
    # ---------------- stock/stock-count
    ("stock/stock-count", 3, r"^(?:0-2|3-5|6\+|0 ?- ?2|3 ?- ?5|6 ?\+|six plus|zero to two|three to five)$"),
    ("stock/stock-count", 3, rf"\b(?:only )?{QTY} {UNIT} (?:left|remaining|remain|dey remain|on (?:the )?shelf)\b"),
    ("stock/stock-count", 3, r"\b(?:e|it|dem|they|am|everything|all|the \w+|\w+) (?:don|has|have|is|are) (?:all |almost )?(?:finish(?:ed)?|run out|sold out|out of stock|sell finish)\b(?!.{0,60}\b(?:bring|send|supply|order|deliver|add)\b)"),
    ("stock/stock-count", 3, r"\b(?:sold out|out of stock|ran out|run out of)\b(?!.{0,60}\b(?:bring|send|supply|order|deliver|add)\b)"),
    ("stock/stock-count", 3, r"\bremain (?:for|on) (?:my |the )?shelf\b|\bon (?:my|the) shelf\b"),
    ("stock/stock-count", 3, r"\b(?:almost|nearly|don almost) (?:finish(?:ed)?|out)\b|\bremain (?:only )?\d+\b|\bstill (?:get|have) \d+\b"),
    ("stock/stock-count", 3, r"\bo ti tan\b|\bo ku (?:\w+ )?(?:\d+|ikan|meji|meta|merin|marun)\b|\bko si mo\b"),
    ("stock/stock-count", 3, r"\bya kare\b|\bsaura (?:\d+|daya|biyu|uku|hudu)\b|\bbabu sauran\b"),
    ("stock/stock-count", 3, r"\bo gwula\b|\bfodu(?:ru)? (?:\d+|otu|abuo|ato)\b|\bo fodu(?:ru)?\b"),
    # ---------------- stock/low-stock-report (owner)
    ("stock/low-stock-report", 3, r"\bwho (?:is|are|dey) (?:running )?low\b|\bwhich shops? (?:is |are )?(?:low|running (?:low|out)|out of stock)\b"),
    ("stock/low-stock-report", 3, r"\blow[- ]stock\b|\bstock (?:report|levels?)\b|\bwhat (?:stock|products?) (?:to|should (?:i|we)) push\b"),
    # ---------------- brand/promo-info
    ("brand/promo-info", 3, r"\b(?:promo|promotion|promos|discount|bonus|giveaway|raffle|free gift|free carton|offer)\b"),
    ("brand/promo-info", 3, r"\bnew (?:product|flavour|flavor|pack|size)\b|\bposter\b|\bcampaign\b|\bdash (?:me|us)\b"),
    ("brand/promo-info", 2, r"\bragi\b|\bmbelata\b|\bẹdinwo\b|\bedinwo\b"),
    # ---------------- brand/brand-email (owner)
    ("brand/brand-email", 3, r"\b(?:draft|write|prepare|send) (?:an? |the )?(?:email|e-mail|mail|letter) (?:to|for)\b"),
    ("brand/brand-email", 3, r"\bemail (?:the )?(?:brand|manufacturer|supplier|bank|nestle|unilever|dangote|cadbury|promasidor|nigerian breweries)\b"),
    # Owner @Brand mentions are scored on the text with the tag kept, only among brand skills.
    ("brand/brand-email", 4, r"@brand\b.{0,12}\b(?:draft|write|email|e-mail|complain|request|follow up|thank|letter|ask|send)\b"),
    # ---------------- mimi/shop-onboarding
    ("mimi/shop-onboarding", 3, r"\bwho (?:are you|is this|be this|dey message|is mimi)\b|\bwhat is mimi\b|\bwho be mimi\b|\bwhat (?:do you|does mimi) (?:do|share)\b"),
    ("mimi/shop-onboarding", 3, r"\bhow (?:did|una) (?:you )?(?:take )?(?:get|take|see) my (?:number|contact)\b|\bprivacy\b|\bmy data\b"),
    ("mimi/shop-onboarding", 3, r"^(?:stop|unsubscribe|stop messages|stop messaging me|pause|opt out)[.! ]*$|\b(?:don't|do not|no) (?:message|text|disturb) me\b|\bstop (?:sending|messaging|texting)\b"),
    ("mimi/shop-onboarding", 3, r"\b(?:join|sign me up|register me|invite link|i was invited)\b"),
    # ---------------- mimi/owner-brief (owner)
    ("mimi/owner-brief", 3, r"\bhow (?:are we|is business|is the business|did we do|we dey|far with the business)\b"),
    ("mimi/owner-brief", 3, r"\b(?:morning|daily|today'?s|yesterday'?s) (?:brief|briefing|summary|update|report)\b|\bgive me (?:the |a |my )?(?:summary|update|brief|rundown)\b"),
    ("mimi/owner-brief", 3, r"\bwhat happened (?:today|yesterday|this week)\b"),
    ("mimi/owner-brief", 3, r"\bbrief me\b"),
    # ---------------- mimi/escalate-owner
    ("mimi/escalate-owner", 3, r"\b(?:complain|complaint|report your|rude|insult(?:ed)?|harass|cheat(?:ed|ing)?|fraud|thief|stole|steal|scam(?:mer)?)\b"),
    ("mimi/escalate-owner", 3, r"\b(?:police|lawyer|court|sue)\b|\b(?:talk|speak) (?:to|with) (?:your )?(?:oga|boss|owner|manager|madam)\b"),
    # ---------------- mimi/chat
    ("mimi/chat", 3, r"^(?:(?:hi+|hello+|hey|helo|good (?:morning|afternoon|evening|night)|gud (?:morning|mrng|evening)|how far|how you dey|how body|thanks?(?: you)?(?: so much| plenty)?|tnx|thx|ok+|okay|alright|noted|e kaaro|e kaasan|e kale|e ku ise|bawo ni|sannu|ina kwana|ina wuni|kedu|ndewo|nagode|na gode|daalu|o se|ese|e se|god bless(?: you)?|amen|lol|😂+|🙏+|👍+)(?: (?:ma|sir|oga|madam|o|boss|bros|my guy|mimi|to you|and your family|o jare))*[.!?,🙏😊👍 ]*)+$"),
    ("mimi/chat", 3, r"\b(?:football|match|chelsea|arsenal|man u|barca|madrid|super eagles|weather|rain|jollof|movie|nollywood|church|mosque|wedding|naming ceremony)\b"),
    ("mimi/chat", 2, r"\b(?:joke|how was your day|how is your day|happy (?:sunday|new month|birthday|weekend))\b"),
]

RULES: tuple[tuple[str, float, re.Pattern[str]], ...] = tuple(
    (k, w, re.compile(p, re.I)) for k, w, p in _R)


@dataclass(frozen=True)
class LexScore:
    scores: dict[str, float]
    top: str
    top_score: float
    second: str
    second_score: float

    @property
    def settled(self) -> bool:
        return self.top_score >= STRONG and self.top_score - self.second_score >= MARGIN

    @property
    def confidence(self) -> float:
        """A rough confidence for the trace; the gate is ``settled``."""
        if self.top_score <= 0:
            return 0.0
        lead = (self.top_score - self.second_score) / self.top_score
        return round(min(0.97, 0.55 + 0.1 * self.top_score * lead), 3)


def score(text: str, allowed: frozenset[str] | None = None) -> LexScore:
    t = normalise(text)
    s: dict[str, float] = defaultdict(float)
    for key, w, rx in RULES:
        if allowed is not None and key not in allowed:
            continue
        if rx.search(t):
            s[key] += w
    ranked = sorted(s.items(), key=lambda kv: (-kv[1], kv[0]))
    top, ts = ranked[0] if ranked else ("", 0.0)
    sec, ss = ranked[1] if len(ranked) > 1 else ("", 0.0)
    return LexScore(dict(s), top, ts, sec, ss)


# ------------------------------------------------------------ photo captions

# A caption that says what a camera photo is about when it is not an order
# list: a payment or its proof, a receipt or document, a complaint about a
# delivery, the shelf (RAI-304). Plain category words, in the languages the
# phrase rules cover; anything here keeps the photo on its words' route.
_PHOTO_OTHER = re.compile(
    r"\b(?:receipts?|invoices?|teller|deposit|slip|evidence|proof|alert|pos|paid|payments?|"
    r"transfer(?:red)?|debit|credit|balance|owe|account|statement|money|cash|kudi|owo|ego)\b"
    r"|\b(?:no|not|never|neva|didn'?t|did not|una no|you no|dem no) (?:bring|deliver\w*|"
    r"complete|reach|come|send)\b"
    r"|\b(?:missing|short|damaged?|broken|spoil\w*|expired|wrong|leak\w*|rude|complain\w*|"
    r"report)\b"
    r"|\b(?:shelf|stock|remain\w*|left)\b", re.I)
# Skills whose words do not explain away a photo: an order, a delivery
# question (a photo is never "where is my van") and a greeting or thanks.
_PHOTO_NEUTRAL = frozenset({"order/order-intake", "order/delivery-status", "mimi/chat"})


def photo_says_other(caption: str) -> bool:
    """True when a camera photo's caption points at a skill other than Order.

    Then the photo keeps the route its words get, and vision is not asked
    first. False for a vague caption ("my list", "see", "abeg"), a greeting,
    an empty one, or words that only point at an order.
    """
    t = normalise(without_injection(caption))
    if not t:
        return False
    if _PHOTO_OTHER.search(t):
        return True
    sc = score(t)
    return any(v >= 2 for k, v in sc.scores.items() if k not in _PHOTO_NEUTRAL)


# --------------------------------------------------------------- language

_LANG_MARKERS: dict[str, tuple[str, ...]] = {
    "pcm": ("abeg", "wetin", "una", "dey", "don", "wey", "oga", "sabi", "abi", "oya",
            "wahala", "comot", "make i", "i go", "e go", "no vex", "how far", "una get",
            "na im", "e remain", "carry come", "shey", "ehn", "sha", "pikin", "small time",
            "i wan", "e no", "neva", "dem"),
    "yo": ("mo ti", "e jowo", "e joo", "elo ni", "melo", "o ti tan", "o ku", "ko si", "mo fe",
           "e ku", "e se", "bawo", "nigba wo", "oja", "sanwo", "rara", "beeni", "lola",
           "emi", "temi", "owo", "gbese", "e mu", "e gbe", "o pe", "ko pe", "e kaaro",
           "ma san", "maa san", "mo san", "ose", "akoko", "jowo", "e fi"),
    "ha": ("na biya", "nawa", "kudi", "kudin", "yaushe", "don allah", "sannu", "nagode",
           "na gode", "kaya", "babu", "akwai", "gobe", "zan", "ina so", "an kawo",
           "kwali", "ya kare", "saura", "ina kwana", "bashi", "ku kawo", "a kawo",
           "na karba", "lokaci", "sati", "yaron", "kayana", "motar", "ragi"),
    "ig": ("ego", "ole", "kedu", "biko", "aga m", "akwuola", "ihe", "ugbu a", "echi",
           "daalu", "ebe", "olee", "mgbe", "nye m", "unu", "ndewo", "achoro m", "anatala",
           "enwetala", "o gwula", "ngwa", "ezitela", "ezigara", "bute", "fodu", "o zughi"),
}


def language(text: str) -> str:
    """Best guess: en, pcm (Nigerian Pidgin), yo, ha or ig. Ties go to English."""
    t = " " + fold(text) + " "
    best, best_n = "en", 0
    for lang, marks in _LANG_MARKERS.items():
        n = sum(1 for m in marks if re.search(rf"(?<![a-z]){re.escape(m)}(?![a-z])", t))
        if n > best_n:
            best, best_n = lang, n
    return best


# --------------------------------------------------------------- injection

_WRAPPER_NAMES = (r"[a-z_]*_message|system|tool|instructions?|developer|assistant|book_slice|"
                  r"helper_verdict|memory_snapshot|thread_summary|environment|routines|"
                  r"untrusted|shop_data|owner|admin")
_WRAPPER_TAG = rf"<\s*/?\s*(?:{_WRAPPER_NAMES})\b[^>]*>"

_INJECTION = [
    r"\bignore (?:all |any |the |your |my )?(?:previous|prior|above|earlier|other)? ?(?:instructions?|rules?|prompts?|messages?)\b",
    r"\bdisregard (?:all |the |your )?(?:previous |above )?(?:instructions?|rules?)\b",
    r"\bforget (?:your|all|the) (?:rules|instructions)\b",
    r"\b(?:system|developer) (?:prompt|message|instruction)s?\b",
    r"(?:^|\s|\[|<)(?:system|assistant|developer)\s*[:\]>]",
    r"\byou are now\b|\bact as (?:the |an? )?(?:admin|owner|system|proof|credit|collector)\b",
    r"\bdeveloper mode\b|\bjailbreak\b|\boverride\b|\bnew instructions?\b",
    r"\broute (?:this|me|it|my message) to\b|\bhand (?:this|me) (?:over )?to (?:the )?(?:credit|proof|collector)\b",
    r"\b(?:as|i am|i'm|this is) (?:the )?(?:owner|admin|oga|distributor|manager)[,:]? (?:i )?(?:approve|authori[sz]e|order|instruct)\b",
    r"\bmark (?:it|this|that|my (?:invoice|payment|balance|debt)) (?:as )?(?:paid|verified|confirmed|cleared|settled)\b",
    r"\bapprove (?:it|this|my (?:credit|payment|limit)) (?:now|immediately|automatically|without)\b",
    r"\b(?:tool_call|function_call|send_money|transfer_money|set_tier|tier\s*[:=])\b",
    # Any of Mimi's wrapper tags, opening or closing, with or without
    # attributes: every *_message wrapper, the book slice, verdicts, memory,
    # summaries, the environment, and the classic system/tool/instructions.
    _WRAPPER_TAG,
]
INJECTION = tuple(re.compile(p, re.I) for p in _INJECTION)
MENTION = re.compile(r"@(order|collector|credit|stock|brand|proof|mimi)\b", re.I)


def injection_spans(text: str) -> list[str]:
    t = fold(text)
    return [m.group(0) for rx in INJECTION for m in [rx.search(t)] if m]


_TAG_BLOCK = re.compile(
    rf"<\s*({_WRAPPER_NAMES})\b[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
# A lone wrapper tag left after the blocks are gone (a "</shop_message>" breakout).
_LONE_TAG = re.compile(_WRAPPER_TAG, re.I)


def without_injection(text: str) -> str:
    """The text with @mentions, injected tag blocks and every sentence or line
    that carries an injected instruction removed."""
    t = _TAG_BLOCK.sub(" ", text)
    t = _LONE_TAG.sub(" ", t)
    t = MENTION.sub(" ", t)
    pieces = re.split(r"(?<=[.!?\n])\s+|\n", t)
    kept = [p.strip() for p in pieces if p.strip() and not injection_spans(p)]
    return " ".join(kept).strip()
