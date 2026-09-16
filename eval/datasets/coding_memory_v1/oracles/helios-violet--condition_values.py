"""Immutable oracle for helios-violet:condition_values."""
import service


def main():
    assert service.retry_budget() == {'limit': 88, 'unit': 'events/hour', 'enabled': True}, service.retry_budget()


if __name__ == "__main__":
    main()
