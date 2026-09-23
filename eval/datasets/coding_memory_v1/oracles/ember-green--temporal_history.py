"""Immutable oracle for ember-green:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-ember-green', service.current_policy()


if __name__ == "__main__":
    main()
