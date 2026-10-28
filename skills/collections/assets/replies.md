# Collections reply wording

Wording from the pilot skill (19 Oct 2026). Fill every {slot} from a tool result: the
naira figures are the tools' *_naira text, copied exactly; the dates come from say_date.
Never send a slot you do not have; leave the sentence out instead.

## Balance
- English: "Good morning {shop}. Your balance with {distributor} is {outstanding_naira}, due {due_words}. Pay to {bank} {account} ({account_name}). I'll send your receipt once it lands."
- Pidgin: "Good morning {shop}. Wetin you owe {distributor} na {outstanding_naira}, e due {due_words}. Pay enter {bank} {account} ({account_name}). I go send receipt once e land."

## Receipt
Code writes the receipt (send_receipt). Do not write your own.

## Reminder
Code writes reminders (send_reminder, propose_reminder_plan) on the overdue ladder. Do not
write your own.

## Good and bad
- Good: Shop: "I don pay 50k." Mimi calls check_payment, finds it, sends the receipt.
- Bad: Shop: "I don pay 50k." Mimi replies "Thank you, confirmed" without check_payment.
