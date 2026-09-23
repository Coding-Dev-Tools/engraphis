"""Immutable oracle for cinder-green:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-cinder-green', service.current_policy()


if __name__ == "__main__":
    main()
