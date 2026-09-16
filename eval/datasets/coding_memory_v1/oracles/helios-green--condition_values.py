"""Immutable oracle for helios-green:condition_values."""
import service


def main():
    assert service.retry_budget() == {'limit': 87, 'unit': 'events/hour', 'enabled': True}, service.retry_budget()


if __name__ == "__main__":
    main()
