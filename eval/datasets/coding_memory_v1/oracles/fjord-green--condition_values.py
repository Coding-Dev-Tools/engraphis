"""Immutable oracle for fjord-green:condition_values."""
import service


def main():
    assert service.retry_budget() == {'limit': 77, 'unit': 'events/hour', 'enabled': True}, service.retry_budget()


if __name__ == "__main__":
    main()
