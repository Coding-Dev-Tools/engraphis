"""Immutable oracle for cinder-green:condition_values."""
import service


def main():
    assert service.retry_budget() == {'limit': 62, 'unit': 'requests/minute', 'enabled': True}, service.retry_budget()


if __name__ == "__main__":
    main()
