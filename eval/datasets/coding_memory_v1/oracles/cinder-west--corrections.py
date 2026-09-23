"""Immutable oracle for cinder-west:corrections."""
import service


def main():
    assert service.current_timeout() == 86, service.current_timeout()


if __name__ == "__main__":
    main()
