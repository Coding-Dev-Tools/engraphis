"""Immutable oracle for helios-west:corrections."""
import service


def main():
    assert service.current_timeout() == 146, service.current_timeout()


if __name__ == "__main__":
    main()
