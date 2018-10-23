from cfn_resource_provider import ResourceProvider
import boto3
import time
from botocore.exceptions import ClientError
import logging
import os

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "DEBUG"))

client = boto3.client('waf')


class RateBasedRuleProvider(ResourceProvider):
    def __init__(self):
        super(RateBasedRuleProvider, self).__init__()
        self.request_schema = {
            "type": "object",
            "required": ["Name", "MetricName", "RateKey", "RateLimit"],
            "properties": {
                "Name": {"type": "string"},
                "MetricName": {"type": "string"},
                "RateKey": {"type": "string"},
                "RateLimit": {"type": "integer"},
                "MatchPredicates": {
                    "Negated": {"type": "boolean"},
                    "Type": {"type": "string",
                             "description": "IPMatch | ByteMatch | SqlInjectionMatch | GeoMatch | SizeConstraint | "
                                            "XssMatch | RegexMatch"},
                    "DataId": {"type": "string"}
                }
            }
        }

    def create(self):
        kwargs = self.properties.copy()

        try:
            kwargs.pop('ServiceToken', None)
            kwargs.pop('MatchPredicates', None)

            kwargs.update({'ChangeToken': client.get_change_token()['ChangeToken']})
            response = client.create_rate_based_rule(**kwargs)

            self.physical_resource_id = response['Rule']['RuleId']

            status = self.wait_on_status(response['ChangeToken'], current_retry=0)   # wait for the rule to finish creating

            if status['Success']:
                if 'Updates' in self.properties:   # check if the rule needs to be updated with predicate(s)
                    print('Predicate(s) detected in create request. Also updating the rule.')
                    self.update()

                    if status['Success']:
                        print('Create and update are done.')
                        self.success('Create and update are done.')
                else:
                    print('Create is done.')
                    self.success('Create is done.')
            else:
                self.fail(status['Reason'])
        except ClientError as error:
            self.physical_resource_id = 'failed-to-create'
            self.fail(f'{error}')

    def update(self, remove_all=False):
        def find_old_predicate(new_pred, old_preds):
            for old_pred in old_preds:
                if new_pred['DataId'] == old_pred['DataId']:
                    return old_pred
                else:
                    return None

        def missing_fields(kwargs):
            required_fields = ['Negated', 'Type', 'DataId']

            return set(required_fields) - set(kwargs)

        old_predicates = {} if 'MatchPredicates' not in self.old_properties else self.old_properties['MatchPredicates']
        print(f"old_predicates: {old_predicates}")

        deletes = []
        inserts = []

        # check for each predicate if it already exists, if so delete it and insert a new one
        if not remove_all:
            new_predicates = self.request['ResourceProperties']['Updates']
            print(f"new_predicates: {new_predicates}")

            for new_predicate in new_predicates:
                missing = missing_fields(new_predicate)
                print(f"missing ==>> {missing}")
                if not missing:
                    old_predicate = find_old_predicate(new_predicate, old_predicates)
                    print(f"old_predicate ==>> {old_predicate}")
                    old_predicates.pop(old_predicate, None)   # remove from predicate list
                    if old_predicate is not None and new_predicate != old_predicate:
                        deletes.append({
                                    'Action': 'DELETE',
                                    'Predicate': old_predicate
                        })
                        inserts.append({
                                    'Action': 'INSERT',
                                    'Predicate': new_predicate
                        })
                    elif old_predicate is None:
                        inserts.append({
                            'Action': 'INSERT',
                            'Predicate': new_predicate
                        })
                else:
                    self.fail(f"Predicate {new_predicate} is missing required fields: {missing}")
                    return

        # delete any predicates that are no longer present in the update request
        for old_predicate in old_predicates:
            deletes.append({
                'Action': 'DELETE',
                'Predicate': old_predicate
            })

        print(f"delete_set: {deletes}")
        print(f"insert_set: {inserts}")

        updates = {'RuleId': self.physical_resource_id}
        updates.update({'RateLimit': self.properties['RateLimit']})
        updates.update({'Updates': [deletes, inserts]})   # merge delete and insert set

        try:
            updates.update({'ChangeToken': client.get_change_token()['ChangeToken']})
            print(f"updates: {updates}")
            response = client.update_rate_based_rule(**updates)

            status = self.wait_on_status(response['ChangeToken'], current_retry=0)   # wait for the rule to finish updating

            if status['Success']:
                print('Update is done.')
                self.success('Update is done.')
            else:
                self.fail(status['Reason'])
        except ClientError as error:
            self.fail(f'{error}')

    def delete(self):
        self.update(remove_all=True)    # remove all predicates

        delete_request = {'RuleId': self.physical_resource_id}

        try:
            delete_request.update({'ChangeToken': client.get_change_token()['ChangeToken']})
            response = client.delete_rate_based_rule(**delete_request)

            status = self.wait_on_status(response['ChangeToken'], current_retry=0)   # wait for the rule to finish deleting

            if status['Success']:
                print('Delete is done.')
                self.success('Delete is done.')
            else:
                self.fail(status['Reason'])
        except ClientError as error:
            if 'WAFNonexistentItemException' in error.response['Error']['Message']:
                self.success()
            else:
                self.fail(f'{error}')

    def wait_on_status(self, change_token, current_retry, interval=30, max_interval=30, max_retries=15):
        try:
            response = client.get_change_token_status(ChangeToken=change_token)

            if response['ChangeTokenStatus'] != 'INSYNC':
                if current_retry >= max_retries:
                    print(f"Max reties ({max_retries}) reached, something must have gone wrong. "
                          f"Current status: {response['ChangeTokenStatus']}.")
                    return {
                        "Success": False,
                        "Reason": f"Max retries ({max_retries}) reached, something must have gone wrong."
                    }
                else:
                    print(f"Not done, current status is: {response['ChangeTokenStatus']}. "
                          f"Waiting {interval} seconds before retrying.")
                    time.sleep(interval)
                    return self.wait_on_status(change_token,
                                               current_retry=current_retry + 1,
                                               interval=min(interval + interval, max_interval))
            else:
                return {"Success": True, "Reason": ""}
        except ClientError as error:
            self.physical_resource_id = 'failed-to-create'
            self.fail(f'{error}')

    def convert_property_types(self):
        self.heuristic_convert_property_types(self.properties)


provider = RateBasedRuleProvider()


def handler(request, context):
    return provider.handle(request, context)
