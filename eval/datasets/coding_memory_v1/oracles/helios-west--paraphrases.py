"""Immutable oracle for helios-west:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-helios-west', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
