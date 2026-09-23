"""Immutable oracle for fjord-north:condition_values."""
import service


def main():
    assert service.retry_budget() == {'limit': 75, 'unit': 'events/hour', 'enabled': True}, service.retry_budget()


if __name__ == "__main__":
    main()
