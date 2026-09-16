"""Immutable oracle for fjord-west:condition_values."""
import service


def main():
    assert service.retry_budget() == {'limit': 76, 'unit': 'events/hour', 'enabled': True}, service.retry_budget()


if __name__ == "__main__":
    main()
