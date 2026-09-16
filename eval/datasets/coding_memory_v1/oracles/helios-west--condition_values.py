"""Immutable oracle for helios-west:condition_values."""
import service


def main():
    assert service.retry_budget() == {'limit': 86, 'unit': 'events/hour', 'enabled': True}, service.retry_budget()


if __name__ == "__main__":
    main()
