"""Explicit, idempotent fixture import. Never restore deleted memories on startup."""
import json
from .models import Customer, Event, MemoryInput
from .store import ROOT, RelationshipStore


def seed_demo(store, data_dir=None):
    seed = json.loads(((data_dir or ROOT/'data/v6')/'relationships.json').read_text())
    existing = {c['id'] for c in store.customers()}
    # A customer fixture is imported only once. No ongoing overwriting of user edits.
    fresh = {c['id'] for c in seed['customers']} - existing
    for customer in seed['customers']:
        if customer['id'] in fresh:
            store.add_customer(Customer.model_validate(customer))
    for event in seed['events']:
        if event['customer_id'] in fresh:
            store.add_event(Event.model_validate(event))
    for memory in seed['memories']:
        if memory['owner_id'] in fresh:
            store.put_memory(MemoryInput.model_validate(memory))
    return {'new_customers':len(fresh),'as_of':seed['as_of']}


if __name__ == '__main__':
    print(seed_demo(RelationshipStore()))
