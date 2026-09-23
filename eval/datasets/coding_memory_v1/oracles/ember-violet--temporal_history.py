"""Immutable oracle for ember-violet:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-ember-violet', service.current_policy()


if __name__ == "__main__":
    main()
