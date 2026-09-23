"""Immutable oracle for ember-north:condition_values."""
import service


def main():
    assert service.retry_budget() == {'limit': 70, 'unit': 'requests/minute', 'enabled': True}, service.retry_budget()


if __name__ == "__main__":
    main()
